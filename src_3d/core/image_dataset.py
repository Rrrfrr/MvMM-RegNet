# -*- coding: utf-8 -*-
"""
Image IO and pre-processing for testing multi-stage registration network of the unified multi-atlas segmentation
framework.

@author: Xinzhe Luo
"""

from __future__ import print_function, division, absolute_import, unicode_literals

import glob
# import random
import itertools
import logging
# import cv2
import os
import nibabel as nib
# from collections import OrderedDict
import numpy as np
from scipy import stats
# import tensorflow as tf
from skimage.transform import rescale
from sklearn import mixture
from torch.utils.data import Dataset, DataLoader

from core import utils
from help.data_augment import randomFilter
# import utils


class ImageDataProvider(Dataset):
    """
    Data provider for the singe-stage multi-atlas segmentation framework training and validation.
    """
    def __init__(self, target_search_path, atlas_search_path=None, a_min=None, a_max=None,
                 image_suffix="image.nii.gz", label_suffix='label.nii.gz', weight_suffix=None,
                 n_atlas=1, train=True, original_size=None, crop_patch=False, random_crop=False,
                 patch_size=(64, 64, 64), crop_roi=False, patch_center=None,
                 channels=1, n_class=2, label_intensity=(0, 205),
                 image_normalization=True, image_augmentation=False, n_subtypes=(2, 1,), scale=0, num_blocks=1,
                 stage='single', target_modality='mr', atlas_modality='mr',
                 image_name_index_begin=0, image_name_index_end=-1, **kwargs):
        """
        :param target_search_path: A glob search pattern to find all target images, labels and probability maps.
        :param atlas_search_path: A glob search pattern to find all atlas images, labels and probability maps.
        :param a_min: (optional) The min value used for clipping.
        :param a_max: (optional) The max value used for clipping.
        :param image_suffix: The suffix pattern for the target images. Default 'image.nii.gz'.
        :param label_suffix: The suffix pattern for the target labels. Default 'label.nii.gz'.
        :param n_atlas: The number of atlases fed into the network.
        :param crop_patch: Whether patches of a certain size need to be cropped for training. Default 'True'.
        :param random_crop: Whether to crop patches randomly.
        :param patch_size: The size of the patch. Default '(64, 64, 64)'.
        :param patch_center: The customized patch center.
        :param crop_roi: Whether to crop ROI containing the whole foreground.
        :param channels: (optional) The number of channels, default=1.
        :param n_class: (optional) The number of classes, default=2.
        :param label_intensity: A tuple of intensities of the ground truth.
        :param image_normalization: Whether to apply image intensity normalization on atlas images; always applied on
            target images as default.
        :param image_augmentation: Whether to apply image data augmentation on target images; do not apply on atlas
            images as default.
        :param n_subtypes: A tuple indicating the number of subtypes within each tissue class, with the first element
            corresponding to the background subtypes.
        :param scale: The scale of the output data.
        :param num_blocks: The number of blocks along each axis.
        :param stage: 'single' for single-stage or 'multi' for multi-stage.
        :param target_modality: The target image modality, either 'mr' or 'ct'.
        :param atlas_modality: The atlas image modality, either 'mr' or 'ct'.
        :param image_name_index_begin: The beginning index of name used as a marker for each target to search the
            corresponding atlas images.
        :param image_name_index_end: The end index of name used as a marker for each target to search the corresponding
            atlas images.
        """
        assert stage in ['single', 'multi'], "The stage must be either 'single' or 'multi'!"
        assert target_modality in ['mr', 'ct'], "The modality must be either 'mr' or 'ct'!"
        assert atlas_modality in ['mr', 'ct'], "The modality must be either 'mr' or 'ct'!"
        self.a_min = a_min if a_min is not None else -np.inf
        self.a_max = a_max if a_min is not None else np.inf
        self.target_search_path = target_search_path
        self.atlas_search_path = atlas_search_path if atlas_search_path else target_search_path
        self.image_suffix = image_suffix
        self.label_suffix = label_suffix
        self.weight_suffix = weight_suffix
        self.n_atlas = n_atlas
        self.train = train
        self.original_size = original_size
        self.crop_patch = crop_patch
        self.random_crop = random_crop
        self.patch_size = np.asarray(patch_size, dtype=np.int16)
        self.crop_roi = crop_roi
        self.patch_center = patch_center
        self.n_class = n_class
        self.channels = channels
        self.label_intensity = label_intensity
        self.n_subtypes = n_subtypes
        self.scale = scale
        self.target_modality = target_modality
        self.atlas_modality = atlas_modality
        self.stage = stage
        self.num_blocks = num_blocks if isinstance(num_blocks, tuple) else (num_blocks,) * 3
        self.image_normalization = image_normalization
        self.image_augmentation = image_augmentation
        self.image_name_index_begin = image_name_index_begin
        self.image_name_index_end = image_name_index_end
        self.kwargs = kwargs
        self.logger = kwargs.pop("logger", logging)

        self.logger.info("Number of atlases for each target: %s" % self.n_atlas)
        # Get all target and atlas image names.
        self.target_atlas_image_names = self._find_data_names(target_search_path, atlas_search_path, image_suffix)

        assert len(self.label_intensity) == self.n_class, "Number of label intensities don't equal to number of " \
                                                          "classes! "

    def __getitem__(self, index):
        target_image_name, atlas_image_names = self.target_atlas_image_names[index]

        target_image, target_affine, target_header = self._load_image_file(target_image_name)
        target_label = self._load_image_file(target_image_name.replace(self.image_suffix,
                                                                       self.label_suffix), order=0)[0]

        atlases_image = np.stack([self._load_image_file(name)[0] for name in atlas_image_names], axis=-1)
        atlases_label = np.stack([self._load_image_file(name.replace(self.image_suffix, self.label_suffix), order=0)[0]
                                 for name in atlas_image_names], axis=-1)  # [*vol_shape, n_atlas]

        if self.weight_suffix:
            target_weight = self._load_image_file(target_image_name.replace(self.image_suffix, self.weight_suffix))[0]
            atlases_weight = np.stack([self._load_image_file(name.replace(self.image_suffix, self.weight_suffix))[0]
                                       for name in atlas_image_names], axis=-2)  # [*vol_shape, n_atlas, n_class]

        # crop roi covering the foreground labels
        if self.crop_roi and not self.crop_patch:
            # Todo: consider whether it is necessary to get ROI coordinates separately on target and atlas
            roi_begin, roi_end = self.get_roi_coordinates(target_label)
            center_percent = (roi_begin + roi_end) // 2 / np.asarray(target_label.shape)
            target_label = target_label[roi_begin[0]:roi_end[0] + 1, roi_begin[1]:roi_end[1] + 1,
                               roi_begin[2]:roi_end[2] + 1]
            target_image = target_image[roi_begin[0]:roi_end[0] + 1, roi_begin[1]:roi_end[1] + 1,
                               roi_begin[2]:roi_end[2] + 1]
            target_image = self._process_image(target_image, self.target_modality,
                                               normalization=True,
                                               augmentation=self.image_augmentation)  # [1, nx, ny, nz, channels]
            target_label = self._process_label(target_label)

            atlases_image = atlases_image[roi_begin[0]:roi_end[0] + 1, roi_begin[1]:roi_end[1] + 1,
                              roi_begin[2]:roi_end[2] + 1, ]
            atlases_label = atlases_label[roi_begin[0]:roi_end[0] + 1, roi_begin[1]:roi_end[1] + 1,
                              roi_begin[2]:roi_end[2] + 1, ]
            atlases_label = np.stack([self._process_label(atlases_label[..., i]) for i in range(self.n_atlas)], axis=-2)

            atlases_image = np.stack([self._process_image(atlases_image[..., i], self.atlas_modality,
                                                          normalization=self.image_normalization,
                                                          augmentation=False)
                                     for i in range(self.n_atlas)], axis=-2)  # [1, *vol_shape, n_atlas, channels]

            if self.weight_suffix:
                target_weight = np.expand_dims(target_weight[roi_begin[0]:roi_end[0]+1, roi_begin[1]:roi_end[1]+1,
                                               roi_begin[2]:roi_end[2]+1], axis=0)
                atlases_weight = np.expand_dims(atlases_weight[roi_begin[0]:roi_end[0]+1, roi_begin[1]:roi_end[1]+1,
                                                roi_begin[2]:roi_end[2]+1], axis=0)

        # crop patches
        elif self.crop_patch:
            # Todo: consider setting patch center separately for target and atlases
            assert np.all(self.patch_size <= np.array(target_label.shape)), 'Patch size %s exceeds dimension size %s!' % (self.patch_size.tolist(), target_label.shape)
            assert np.all(self.patch_size <= np.array(atlases_label.shape[:-1])), 'Patch size %s exceeds dimension size %s!' % (self.patch_size.tolist(), atlases_label.shape[:-1])
            # crop a fixed patch centered at the given patch center
            if self.patch_center:
                patch_center = np.asarray(self.patch_center, dtype=np.int16)
            # crop random patches
            elif self.random_crop and not self.crop_roi:
                patch_center = np.asarray([np.random.randint(self.patch_size[i] // 2,
                                                             target_label.shape[i] + self.patch_size[i] // 2
                                                             - self.patch_size[i] + 1) for i in
                                           range(target_label.ndim)], dtype=np.int16)
            # crop random patches that covers the foreground region
            elif self.random_crop and self.crop_roi:
                roi_center = self._get_foreground_center(target_label)
                patch_center = np.asarray([np.random.randint(max(roi_center[i] - self.patch_size[i] + self.patch_size[i] // 2 + 1,
                                                                 self.patch_size // 2),
                                                             min(roi_center[i] + self.patch_size[i] // 2,
                                                                 target_label.shape[i] + self.patch_size[i] // 2 - self.patch_size[i] + 1))
                                           for i in range(target_label.ndim)], dtype=np.int16)
            # crop a fixed patch centered at the foreground center
            elif not self.random_crop and self.crop_roi:
                patch_center = self._get_foreground_center(target_label)
            else:
                target_patch_center = np.asarray(target_image.shape, dtype=np.int16) // 2
                atlas_patch_center = np.asarray(atlases_image.shape[:-1], np.int16) // 2

            # target slicing indices
            target_begin = target_patch_center - self.patch_size // 2
            target_end = target_patch_center - self.patch_size // 2 + self.patch_size
            center_percent = target_patch_center / np.asarray(target_label.shape)

            # atlas slicing indices
            atlas_begin = atlas_patch_center - self.patch_size // 2
            atlas_end = atlas_patch_center - self.patch_size // 2 + self.patch_size

            target_label = target_label[target_begin[0]:target_end[0], target_begin[1]:target_end[1],
                            target_begin[2]:target_end[2]]
            target_image = target_image[target_begin[0]:target_end[0], target_begin[1]:target_end[1],
                            target_begin[2]:target_end[2]]
            target_image = self._process_image(target_image, self.target_modality,
                                               normalization=True,
                                               augmentation=self.image_augmentation)  # [1, nx, ny, nz, channels]
            target_label = self._process_label(target_label)

            atlases_image = atlases_image[atlas_begin[0]:atlas_end[0], atlas_begin[1]:atlas_end[1],
                            atlas_begin[2]:atlas_end[2], ]
            atlases_label = atlases_label[atlas_begin[0]:atlas_end[0], atlas_begin[1]:atlas_end[1],
                            atlas_begin[2]:atlas_end[2], ]
            atlases_label = np.stack([self._process_label(atlases_label[..., i]) for i in range(self.n_atlas)], axis=-2)
            # self.logger.info(atlases_image.shape)
            atlases_image = np.stack([self._process_image(atlases_image[..., i], self.atlas_modality,
                                                          normalization=self.image_normalization,
                                                          augmentation=False)
                                     for i in range(self.n_atlas)], axis=-2)

            if self.weight_suffix:
                target_weight = np.expand_dims(target_weight[target_begin[0]:target_end[0],
                                               target_begin[1]:target_end[1],
                                               target_begin[2]:target_end[2]], axis=0)
                atlases_weight = np.expand_dims(atlases_weight[atlas_begin[0]:atlas_end[0],
                                                atlas_begin[1]:atlas_end[1],
                                                atlas_begin[2]:atlas_end[2]], axis=0)

        else:
            target_image = self._process_image(target_image, self.target_modality,
                                               normalization=True,
                                               augmentation=self.image_augmentation)  # [1, nx, ny, nz, channels]
            target_label = self._process_label(target_label)
            center_percent = np.asarray((1 / 2, 1 / 2, 1 / 2))

            atlases_label = np.stack([self._process_label(atlases_label[..., i]) for i in range(self.n_atlas)], axis=-2)
            atlases_image = np.stack([self._process_image(atlases_image[..., i], self.atlas_modality,
                                                          normalization=self.image_normalization,
                                                          augmentation=False)
                                     for i in range(self.n_atlas)], axis=-2)
            if self.weight_suffix:
                target_weight = np.expand_dims(target_weight, 0)
                atlases_weight = np.expand_dims(atlases_weight, 0)

        target_image = utils.crop_into_blocks(target_image, n_blocks=self.num_blocks,
                                                 block_size=self.patch_size, output_type='array')
        target_label = utils.crop_into_blocks(target_label, n_blocks=self.num_blocks,
                                                 block_size=self.patch_size, output_type='array')
        target_weight = utils.crop_into_blocks(target_weight, n_blocks=self.num_blocks,
                                                  block_size=self.patch_size, output_type='array') \
                            if self.weight_suffix else np.ones_like(target_label)
        atlases_image = utils.crop_into_blocks(atlases_image, n_blocks=self.num_blocks,
                                                  block_size=self.patch_size, output_type='array')
        atlases_label = utils.crop_into_blocks(atlases_label, n_blocks=self.num_blocks,
                                                  block_size=self.patch_size, output_type='array')
        atlases_weight = utils.crop_into_blocks(atlases_weight, n_blocks=self.num_blocks,
                                                   block_size=self.patch_size, output_type='array') \
                            if self.weight_suffix else np.ones_like(atlases_label)

        return {'target_image': target_image,
                'target_label': target_label,
                'target_weight': target_weight,
                'target_affine': target_affine,
                'target_header': target_header,
                'atlases_image': atlases_image,
                'atlases_label': atlases_label,
                'atlases_weight': atlases_weight,
                'center_percent': center_percent}

    def __len__(self):
        return len(self.target_atlas_image_names)

    def get_image_names(self, index):
        return self.target_atlas_image_names[index]

    def _find_data_names(self, target_search_path, atlas_search_path, name_suffix):
        all_target_files = utils.strsort(glob.glob(target_search_path))
        all_atlas_files = utils.strsort(glob.glob(atlas_search_path))

        target_names = [name for name in all_target_files if name_suffix in name]
        assert len(target_names) > 0, "No training targets!"
        atlas_names = [name for name in all_atlas_files if name_suffix in name]

        self.logger.info("Number of targets: %s, loaded from directory: %s" % (len(target_names), target_search_path))
        self.logger.info("Number of atlases: %s, loaded from directory: %s" % (len(atlas_names), atlas_search_path))

        if self.stage == 'multi':
            all_names = []
            for target_name in target_names:
                t_name = os.path.basename(target_name)
                # screen the atlas names for each target
                a_names = [name for name in atlas_names
                           if 'target-' + t_name[self.image_name_index_begin:self.image_name_index_end] in name]
                # list all combinations of atlases
                comb_atlas_names = list(itertools.combinations(a_names, self.n_atlas))
                self.logger.info("Number of available atlases combinations "
                                 "for target %s: %s" % (t_name, len(comb_atlas_names)))
                # cartesian product to make pairs of target and atlas names
                all_names.append(list(itertools.product([target_name], comb_atlas_names)))

            target_atlas_names = list(itertools.chain(*all_names))

        elif self.stage == "single":
            comb_atlas_names = list(itertools.combinations(atlas_names, self.n_atlas))

            self.logger.info("Number of available atlases combinations "
                             "for each target: %s" % len(comb_atlas_names))

            # cartesian product to make pairs of target and atlas names
            target_atlas_names = list(itertools.product(target_names, comb_atlas_names))

        return target_atlas_names

    def _load_image_file(self, path, dtype=np.float32, order=1):
        img = nib.load(path)
        image = np.asarray(img.get_fdata(), dtype)
        if self.scale > 0:
            image = rescale(image, 1 / (2 ** self.scale), mode='reflect',
                            multichannel=False, anti_aliasing=False, order=order)
        return image, img.affine, img.header

    def _load_prob_file(self, path_list, dtype=np.float32, max_value=1000):
        return np.asarray(np.stack([self._load_image_file(name, order=0)[0]
                                    for name in path_list], -1) / max_value,
                          dtype=dtype)

    def _process_label(self, gt):
        """
        Process ground-truths into one-hot representation.

        :param gt: A ground-truth array, of shape [nx, ny, nz].
        :return: An array of one-hot representation, of shape [1, nx, ny, nz, n_class].
        """
        gt = np.around(gt)
        label = np.zeros((np.hstack((gt.shape, self.n_class))), dtype=np.float32)

        for k in range(1, self.n_class):
            label[..., k] = (gt == self.label_intensity[k])

        label[..., 0] = np.logical_not(np.sum(label[..., 1:], axis=-1))

        return np.expand_dims(label, 0)

    def _process_image(self, data, modality, normalization=True, augmentation=False):
        """
        Process data with z-score normalization.

        :param data: An input data array, of shape [nx, ny, nz].
        :return: An array that is z-score normalized, of shape [1, nx, ny, nz, n_channel].
        """
        if modality == 'mr':
            data_clip = np.clip(data, -np.inf, np.percentile(data, 99))

        elif modality == 'ct':
            data_clip = np.clip(data, self.a_min, self.a_max)

        if augmentation:
            data_aug = randomFilter(data_clip)
        else:
            data_aug = data_clip

        if normalization:
            method = self.kwargs.pop("normalization_method", 'z-score')
            if method == 'z-score':
                # z-score normalization
                data_norm = stats.zscore(data_aug, axis=None, ddof=1)
            elif method == 'min-max':
                # min-max normalization
                data_norm = data_aug - np.min(data_aug)
                data_norm = data_norm / np.max(data_norm)
            else:
                raise ValueError("Unknown normalization method: %s" % method)
            data_expand = np.expand_dims(data_norm, axis=-1)
        else:
            data_expand = np.expand_dims(data_aug, axis=-1)

        return np.expand_dims(np.tile(data_expand, np.hstack((np.ones(data.ndim), self.channels))), 0)

    def _get_random_patch_center_covering_foreground(self, label, margin=(20, 20, 20)):
        """
        Crop random patches that cover the foreground.
        :param label: The label to crop patches.
        :param margin: The margin between the patch center and the foreground area.
        :return: A random patch center.
        """
        foreground_flag = np.any(np.concatenate(tuple([(np.expand_dims(label, axis=0) == k) for k in
                                                       self.label_intensity[1:]])), axis=0)
        arg_index = np.argwhere(foreground_flag)  # [n, 3]


    def _get_foreground_center(self, label):
        """
        Compute the center coordinates of the label according to the given label intensities.

        :param label: The label to derive center coordinates.
        :return: An array representing the foreground center coordinates, of shape [3].
        """
        foreground_flag = np.any(np.concatenate(tuple([(np.expand_dims(label, axis=0) == k) for k in
                                                       self.label_intensity[1:]])), axis=0)
        return np.floor(np.mean(np.stack(np.where(foreground_flag)), -1)).astype(np.int16)

    def get_roi_coordinates(self, label, mag_rate=0.1):
        """
        Produce the cuboid ROI coordinates representing the opposite vertices.

        :param label: A ground-truth label image.
        :param mag_rate: The magnification rate for ROI cropping.
        :return: An array representing the smallest coordinates of ROI;
            an array representing the largest coordinates of ROI.
        """

        foreground_flag = np.any(np.concatenate(tuple([(np.expand_dims(label, axis=0) == k) for k in
                                                       self.label_intensity[1:]])), axis=0)
        arg_index = np.argwhere(foreground_flag)

        low = np.min(arg_index, axis=0)
        high = np.max(arg_index, axis=0)

        soft_low = np.maximum(np.floor(low - (high - low) * mag_rate / 2), np.zeros_like(low))
        soft_high = np.minimum(np.floor(high + (high - low) * mag_rate / 2), np.asarray(label.shape) - 1)

        return soft_low, soft_high

    def _get_mixture_coefficients(self, image, label):
        """
        Get the image mixture coefficients of each subtype within the tissue class.

        :param image: The image array of shape [1, nx, ny, nz, channels].
        :param label: The label array of shape [1, nx, ny, nz, n_class].
        :return: tau - a list of arrays of shape [1, n_subtypes[i]];
                 mu - a list of arrays of shape [1, n_subtypes[i]];
                 sigma - a list of arrays of shape [1, n_subtypes[i]].
        """
        tau = []
        mu = []
        sigma = []
        for i in range(self.n_class):
            image_take = np.take(np.sum(image, axis=-1).flatten(), indices=np.where(label[..., i].flatten() == 1))
            clf = mixture.GaussianMixture(n_components=self.n_subtypes[i])
            clf.fit(image_take.reshape(-1, 1))
            tau.append(np.expand_dims(clf.weights_, 0))
            mu.append(np.expand_dims(clf.means_.squeeze(1), 0))
            sigma.append(np.expand_dims(np.sqrt(clf.covariances_.squeeze((1, 2))), 0))
        return tau, mu, sigma

    def _process_atlas(self, atlas):
        """
        Convert an atlas into a probabilistic one.

        :param atlas: of shape [nx, ny, nz]
        :return: The probabilistic atlas, of shape [1, nx, ny, nz, n_class].
        """
        binary_atlas = self._process_label(atlas)  # [1, nx, ny, nz, n_class]
        return atlas

    def _post_process(self, data, labels):
        """
        Post processing hook that can be used for data augmentation.

        :param data: the data array
        :param labels: the label array
        """

        '''
        data = tf.convert_to_tensor(data)
        labels = tf.convert_to_tensor(labels)

        concat_image = tf.concat([tf.expand_dims(data, 2), labels], axis=-1)

        maybe_flipped = tf.image.random_flip_left_right(concat_image)
        maybe_flipped = tf.image.random_flip_up_down(maybe_flipped)

        data = maybe_flipped[:, :, :1]
        labels = maybe_flipped[:, :, 1:]

        data = tf.image.random_brightness(data, 0.2)
        #labels = tf.image.random_brightness(labels, 0.7)
        '''
        return data, labels

    def collate_fn(self, batch):
        res_list = batch

        TI = np.concatenate([res['target_image'] for res in res_list], axis=0)
        TL = np.concatenate([res['target_label'] for res in res_list], axis=0)
        TW = np.concatenate([res['target_weight'] for res in res_list], axis=0)
        AF = [res['target_affine'] for res in res_list]
        HE = [res['target_header'] for res in res_list]
        AI = np.concatenate([res['atlases_image'] for res in res_list], axis=0)
        AL = np.concatenate([res['atlases_label'] for res in res_list], axis=0)
        AW = np.concatenate([res['atlases_weight'] for res in res_list], axis=0)
        CP = np.concatenate([res['center_percent'] for res in res_list], axis=0)

        return {'target_image': TI, 'target_label': TL, 'target_affine': AF,
                'target_header': HE, 'target_weight': TW,
                'atlases_image': AI, 'atlases_label': AL, 'atlases_weight': AW,
                'center_percent': CP}
        # return TI, TL, TP, AF, AI, AP


if __name__ == "__main__":
    import time

    epochs = 2
    iterations = 400
    batch_size = 1
    num_workers = 0
    patch_size = (96, 96, 96)

    data_provider = ImageDataProvider(target_search_path='../../../../dataset/training_mr_20_commonspace2/*.nii.gz',
                                      atlas_search_path='../../../../dataset/training_mr_20_commonspace2/*.nii.gz',
                                      image_suffix='image.nii.gz',
                                      label_suffix='label.nii.gz',
                                      n_atlas=1,
                                      crop_patch=True,
                                      patch_size=patch_size,
                                      crop_roi=False,
                                      random_crop=False,
                                      channels=1,
                                      n_class=8,
                                      label_intensity=(0, 205, 420, 500, 550, 600, 820, 850),
                                      n_subtypes=(2, 1, 1, 1, 1, 1, 1, 1),
                                      scale=0)

    print("Length of the data provider: %s" % len(data_provider))

    for i in range(len(data_provider)):
        print(data_provider.target_atlas_image_names[i])
        print(np.all(data_provider[i]['target_image'] == data_provider[i]['atlases_image'].squeeze(-2)))
        print(data_provider[i]['target_image'].shape)

    data_loader = DataLoader(data_provider, batch_size=batch_size, shuffle=True, num_workers=num_workers,
                             collate_fn=data_provider.collate_fn)

    t_start = time.time()

    target_images = []
    for i in range(epochs):
        print("Epoch %s" % i)

        for step, batch in enumerate(data_loader):
            print("Iteration %s" % step)
            print(np.all(batch['target_image'] == batch['atlases_image'].squeeze(-2)))
            print(np.all(batch['target_label'] == batch['atlases_label'].squeeze(-2)))
            if step == 0:
                target_images.append(batch['target_image'])

    print(np.all(target_images[0] == target_images[1]))

    t_end = time.time()

    print("The average running time for data loading is: %s" % ((t_end - t_start) / (epochs * iterations)))

