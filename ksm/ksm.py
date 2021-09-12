import json
import os
from pyphot import unit, Filter
import torch

from ksm.cvae import CVAE
import numpy as np


class Model:
    """
    Base class for the kilonova surrogate model
    """

    def __init__(
        self,
        metadata_file_path,
        pytorch_weights_file_path,
        filter_library_path=None,
        observations=None,
    ):
        """
        :param metadata_file_path: Metadata json file (see README for specs)
        :type metadata_file_path: str
        :param filter_library_path: path to dir of filter profile .dat files to load
        :type filter_library_path: str, optional
        :param observations: an Observations object that will always be used, otherwise times, filters, etc.
        must be specified anew each time Model.predict_magnitudes() is called
        :type observations: class `ksm.observations.Observations`, optional
        """
        with open(metadata_file_path) as json_file:
            metadata = json.load(json_file)

        self.latent_units = metadata["latent_units"]
        self.hidden_units = metadata["hidden_units"]
        # self.model_wavelengths = np.load(metadata["path_to_wavelengths"])
        if metadata["wavelengths_style"] == "bulla":
            self.model_wavelengths = np.linspace(100.0, 99900, 500)
        elif metadata["wavelengths_style"] == "kasen":
            """
            Using this function for the wavelengths introduces an error on the order of 10^-6 
            in the magnitudes in bands and therefore I do not care about it. This function
            was generated by np.polyfit on log10 of the wavelengths originally included in the
            github hdf5 files of D. Kasen. If this is unacceptable to whoever reads this comment,
            likely years from now, you can get the wavelengths from the original data files on github, save
            as a .npy file, and instead of the below function, use np.load(). 
            """
            self.model_wavelengths = 10 ** (
                2.175198139181011 + np.linspace(0, 1, 1629) * 2.8224838828121763
            )
        self.spectrum_size = len(self.model_wavelengths)
        self.input_size = metadata["input_size"]
        self.x_transforms = {int(k): v for k, v in metadata["x_transforms"].items()}
        self.x_transform_rules = metadata["x_transforms_exp_rules"]
        self.y_transforms = metadata["y_transforms"]
        self.num_samples = 1
        self.model_type = metadata["wavelengths_style"]

        self.nn_model = CVAE(
            self.spectrum_size, self.hidden_units, self.latent_units, self.input_size
        )
        # This will throw an error if you've provided a model of the wrong size
        self.nn_model.load_state_dict(
            torch.load(pytorch_weights_file_path, map_location=torch.device("cpu"))
        )
        self.nn_model.eval()

        # read in filters, if specified
        # filename must end with .dat and to access the filter later, use the filename sans .dat
        if filter_library_path:
            self.filter_library = {}
            for f in os.listdir(filter_library_path):
                if f.endswith("dat"):
                    filter_data = np.loadtxt(os.path.join(filter_library_path, f))
                    pyphot_filter = Filter(
                        filter_data.T[0] * unit["AA"],
                        filter_data.T[1],
                        name=f[:-4],
                        dtype="photon",
                        unit="Angstrom",
                    )
                    self.filter_library[f[:-4]] = pyphot_filter
            self.filters_loaded = True
        else:
            self.filters_loaded = False

        if observations is not None:
            self.observations = observations
        else:
            self.observations = None

    def predict_spectra(self, physical_parameters, times):
        unique_times = np.unique(times)
        all_data_input = np.hstack(
            (
                np.repeat(
                    physical_parameters.reshape(1, -1), len(unique_times), axis=0
                ),
                unique_times.reshape(-1, 1),
            )
        )
        nn_input = self.physical_inputs_to_nn(all_data_input)
        with torch.no_grad():
            z = (
                torch.zeros((1, self.latent_units))
                .repeat((len(nn_input), 1))
                .to(torch.float)
            )
            decoder_input = torch.cat(
                (z, torch.from_numpy(nn_input).to(torch.float)), dim=1
            )
            reconstructions = self.nn_model.decoder(decoder_input)
        reconstructions_np = reconstructions.double().cpu().detach().numpy()
        spectra_nn = self.spectra_to_real_units(reconstructions_np)
        return spectra_nn, unique_times

    def predict_magnitudes(
        self, physical_parameters, times=None, filters=None, distance=None
    ):
        """

        :param physical_parameters:
        :param times:
        :param filters:
        :param distance: centimetres
        :return:
        """
        if self.observations is not None:
            # Using the Observations object inputs
            spectra_from_nn, unique_times = self.predict_spectra(
                physical_parameters, self.observations.times
            )
            spectra_at_distance = spectra_from_nn / (
                4 * np.pi * self.observations.distance ** 2
            )
            magnitudes = np.empty_like(self.observations.times)

            for f in self.observations.filters_unique:
                filter_indices = np.where(self.observations.filters == f)
                times_of_filter = self.observations.times[filter_indices]
                spectra_of_filter = spectra_at_distance[
                    np.searchsorted(unique_times, times_of_filter)
                ]
                ff = self.filter_library[f]
                flux = ff.get_flux(
                    self.model_wavelengths * unit["AA"],
                    spectra_of_filter * unit["flam"],
                )
                mag = -2.5 * np.log10(flux) - ff.AB_zero_mag
                magnitudes[filter_indices] = mag

        elif times is not None:
            spectra_from_nn, unique_times = self.predict_spectra(
                physical_parameters, times
            )
            spectra_at_distance = spectra_from_nn / (4 * np.pi * distance ** 2)
            magnitudes = np.empty_like(times)
            filters_unique = np.unique(filters)

            for f in filters_unique:
                filter_indices = np.where(filters == f)
                times_of_filter = times[filter_indices]
                spectra_of_filter = spectra_at_distance[
                    np.searchsorted(unique_times, times_of_filter)
                ]
                ff = self.filter_library[f]
                flux = ff.get_flux(
                    self.model_wavelengths * unit["AA"],
                    spectra_of_filter * unit["flam"],
                )
                mag = -2.5 * np.log10(flux) - ff.AB_zero_mag
                magnitudes[filter_indices] = mag

        else:
            raise ValueError(
                "Neither Observations object nor times post merger to predict at specified."
            )

        return magnitudes

    def physical_inputs_to_nn(self, param_matrix):
        param_matrix_new = np.empty_like(param_matrix.T)
        for i, rule in zip(range(len(param_matrix.T)), self.x_transform_rules):
            if not rule:
                param_matrix_new[i] = (param_matrix.T[i] - self.x_transforms[i][0]) / (
                    self.x_transforms[i][1] - self.x_transforms[i][0]
                )
            if rule:
                if i == 2 and self.model_type == "kasen":
                    param_matrix_new[i] = (
                        -1 * np.log10(param_matrix.T[i]) - self.x_transforms[i][0]
                    ) / (self.x_transforms[i][1] - self.x_transforms[i][0])
                else:
                    param_matrix_new[i] = (
                        np.log10(param_matrix.T[i]) - self.x_transforms[i][0]
                    ) / (self.x_transforms[i][1] - self.x_transforms[i][0])

        return param_matrix_new.T

    def spectra_to_real_units(self, y):
        # returns erg/s/Hz
        return (
            10.0
            ** (
                y * (self.y_transforms[2] - self.y_transforms[1]) + self.y_transforms[1]
            )
            - self.y_transforms[0]
        )
