import json
import os
from pyphot import (unit, Filter)
import torch

from ksm.cvae import CVAE
import numpy as np


class Model:
    """
    Base class for the kilonova surrogate model
    """
    def __init__(self, metadata_file_path, filter_library_path=None, observations=None):
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
        self.model_wavelengths = np.load(metadata["path_to_wavelengths"])
        self.spectrum_size = len(self.model_wavelengths)
        self.input_size = metadata["input_size"]
        self.x_transforms = {int(k): v for k, v in metadata["x_transforms"].items()}
        self.x_transform_rules = metadata["x_transforms_exp_rules"]
        self.y_transforms = metadata["y_transforms"]
        self.num_samples = 10

        self.nn_model = CVAE(self.spectrum_size, self.hidden_units, self.latent_units, self.input_size)
        self.nn_model.load_state_dict(torch.load(metadata["path_to_pytorch_weights"],
                                                 map_location=torch.device('cpu')))
        self.nn_model.eval()

        # read in filters, if specified
        # filename must end with .dat and to access the filter later, use the filename sans .data
        if filter_library_path:
            self.filter_library = {}
            for f in os.listdir(filter_library_path):
                if f.endswith('dat'):
                    filter_data = np.loadtxt(os.path.join(filter_library_path, f))
                    pyphot_filter = Filter(filter_data.T[0] * unit['AA'],
                                           filter_data.T[1],
                                           name=f[:-4],
                                           dtype='photon',
                                           unit='Angstrom')
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
        all_data_input = np.hstack((np.repeat(physical_parameters.reshape(1, -1),
                                              len(unique_times), axis=0),
                                    unique_times.reshape(-1, 1)))
        nn_input = self.physical_inputs_to_nn(all_data_input)

        with torch.no_grad():
            reconstructions = torch.empty((len(nn_input), self.spectrum_size,
                                           self.num_samples)).to(torch.float)
            for i in range(self.num_samples):
                z = torch.randn(1, self.latent_units)
                Z = z.repeat((len(nn_input), 1)).to(torch.float)
                decoder_input = torch.cat((Z, torch.from_numpy(nn_input).to(torch.float)), dim=1)
                reconstruction = self.nn_model.decoder(decoder_input)
                reconstructions[:, :, i] = reconstruction
        reconstructions_np = reconstructions.cpu().detach().numpy()
        spectra_nn = self.spectra_to_real_units(np.mean(reconstructions_np, 2, dtype='double'))
        return spectra_nn, unique_times

    def predict_magnitudes(self, physical_parameters, times=None, filters=None, distance=None):
        """

        :param physical_parameters:
        :param times:
        :param filters:
        :param distance: centimetres
        :return:
        """
        if self.observations is not None:
            # Using the Observations object inputs
            spectra_from_nn, unique_times = self.predict_spectra(physical_parameters, self.observations.times)
            spectra_at_distance = spectra_from_nn / (4 * np.pi * self.observations.distance ** 2)
            magnitudes = np.empty_like(self.observations.times)

            for f in self.observations.filters_unique:
                filter_indices = np.where(self.observations.filters == f)
                times_of_filter = self.observations.times[filter_indices]
                spectra_of_filter = spectra_at_distance[np.searchsorted(unique_times, times_of_filter)]
                ff = self.filter_library[f]
                flux = ff.get_flux(self.model_wavelengths * unit['AA'], spectra_of_filter * unit['flam'])
                mag = -2.5 * np.log10(flux) - ff.AB_zero_mag
                magnitudes[filter_indices] = mag

        elif times is not None:
            spectra_from_nn, unique_times = self.predict_spectra(physical_parameters, times)
            spectra_at_distance = spectra_from_nn / (4 * np.pi * distance ** 2)
            magnitudes = np.empty_like(times)
            filters_unique = np.unique(filters)

            for f in filters_unique:
                filter_indices = np.where(filters == f)
                times_of_filter = times[filter_indices]
                spectra_of_filter = spectra_at_distance[np.searchsorted(unique_times, times_of_filter)]
                ff = self.filter_library[f]
                flux = ff.get_flux(self.model_wavelengths * unit['AA'], spectra_of_filter * unit['flam'])
                mag = -2.5 * np.log10(flux) - ff.AB_zero_mag
                magnitudes[filter_indices] = mag

        else:
            # raise something to say that either an Observations object required or required to
            # specify data in correct format
            raise ValueError("Neither Observations object nor times post merger to predict at specified.")

        return magnitudes

    def physical_inputs_to_nn(self, param_matrix):
        param_matrix_new = np.empty_like(param_matrix.T)
        for i, rule in zip(range(len(param_matrix.T)), self.x_transform_rules):
            if not rule:
                param_matrix_new[i] = (param_matrix.T[i] -
                                       self.x_transforms[i][0]) / (self.x_transforms[i][1] - self.x_transforms[i][0])
            if rule:
                param_matrix_new[i] = np.log10((param_matrix.T[i] -
                                                self.x_transforms[i][0]) / (
                                                       self.x_transforms[i][1] - self.x_transforms[i][0]))

        return param_matrix_new.T

    def spectra_to_real_units(self, y):
        # returns erg/s/Hz
        return 10. ** (y * (self.y_transforms[2] - self.y_transforms[1]) + self.y_transforms[1]) - self.y_transforms[0]

    def compute_likelihood_dynesty(self, physical_params):
        """
        This method requires self.observations is not None but I'm not checking for it because this is a function
        that dynesty would evaluate over and over again so.

        Also this function accounts for the upper limit measurements (as instructed by Z.Doctor)
        :param physical_parameters:
        :return: Gaussian log likelihood (plus 1 for the "modelling uncertainty" ala Coughlin and co.)
        """
        magnitude_predictions = self.predict_magnitudes(physical_params, times=self.observations.times,
                                                        filters=self.observations.filters,
                                                        distance=self.observations.distance)
        for i in self.observations.upper_limit_indices:
            if magnitude_predictions[i] < self.observations.data_magnitudes[i]:
                return -np.inf
        loglklhd = -0.5 * np.sum((magnitude_predictions - self.observations.data_magnitudes) ** 2 / (self.observations.magnitude_errors + 1 ) ** 2)
        return loglklhd

    def prior_transform_dynesty(self, uniform_params):
        """
        A dynesty thing.
        :param uniform_params:
        :return:
        """
        non_uniform = []
        for i, param in enumerate(uniform_params):
            non_uniform.append(param * (self.x_transforms[i][0] - self.x_transforms[i][1]) + self.x_transforms[i][1])
        return non_uniform

    def log_prior_emcee(self, physical_params):
        """
        Accounts for the parameters being within the correct ranges
        :param physical_params:
        :return:
        """
        for i, param in enumerate(physical_params):
            if self.x_transforms[i][0] < param < self.x_transforms[i][1]:
                continue
            else:
                return -np.inf
        return 0.0

    def log_probability_emcee(self, physical_params):
        """
        Requires self.observations is not None
        :param physical_params:
        :return: Gauss
        """
        prior = self.log_prior_emcee(physical_params)

        if not np.isfinite(prior):
            return -np.inf

        magnitude_predictions = self.predict_magnitudes(physical_params, times=self.observations.times,
                                                        filters=self.observations.filters,
                                                        distance=self.observations.distance)
        for i in self.observations.upper_limit_indices:
            if magnitude_predictions[i] < self.observations.data_magnitudes[i]:
                return -np.inf
        loglklhd = -0.5 * np.sum((magnitude_predictions - self.observations.data_magnitudes) ** 2 / (self.observations.magnitude_errors + 1 ) ** 2)
        return loglklhd