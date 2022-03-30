# Copyright (C) 2022 Intel Corporation
#
# SPDX-License-Identifier: MIT

import logging as log
import os
import os.path as osp
import sys
from importlib.resources import open_text
from multiprocessing import Pool
from random import Random
from typing import List, Optional, Tuple

import cv2 as cv
import numpy as np
import requests

from datumaro.components.dataset_generator import DatasetGenerator
from datumaro.util.image import save_image

from .utils import IFSFunction, augment, colorize


class FractalImageGenerator(DatasetGenerator):
    """
    ImageGenerator generates 3-channel synthetic images with provided shape.
    Uses the algorithm from the article: https://arxiv.org/abs/2103.13023
    """

    _MODEL_PROTO_NAME = "colorization_deploy_v2.prototxt"
    _MODEL_CONFIG_NAME = "colorization_release_v2.caffemodel"
    _HULL_PTS_FILE_NAME = "pts_in_hull.npy"
    _COLORS_FILE = "background_colors.txt"

    def __init__(
        self, output_dir: str, count: int, shape: Tuple[int, int], model_path: Optional[str] = None
    ) -> None:
        assert 0 < count, "Image count cannot be lesser than 1"
        self._count = count

        self._output_dir = output_dir
        self._model_dir = model_path if model_path else os.getcwd()

        self._cpu_count = min(os.cpu_count(), self._count)

        assert len(shape) == 2
        self._height, self._width = shape

        self._weights = self._create_weights(IFSFunction.NUM_PARAMS)
        self._threshold = 0.2
        self._iterations = 200000
        self._num_of_points = 100000

        self._download_colorization_model(self._model_dir)

        self._initialize_params()

    def generate_dataset(self) -> None:
        log.info(
            "Generation of '%d' 3-channel images with height = '%d' and width = '%d'",
            self._count,
            self._height,
            self._width,
        )

        # On Mac 10.15 and Python 3.7 the use of multiprocessing leads to hangs
        use_multiprocessing = sys.platform != "darwin" or sys.version_info >= (3, 8)
        if use_multiprocessing:
            with Pool(processes=self._cpu_count) as pool:
                params = pool.map(
                    self._generate_category, [Random(i) for i in range(self._categories)]
                )
        else:
            params = []
            for i in range(self._categories):
                param = self._generate_category(Random(i))
                params.append(param)

        instances_weights = np.repeat(self._weights, self._instances, axis=0)
        weight_per_img = np.tile(instances_weights, (self._categories, 1))
        params = np.array(params, dtype=object)
        repeated_params = np.repeat(params, self._weights.shape[0] * self._instances, axis=0)
        repeated_params = repeated_params[: self._count]
        weight_per_img = weight_per_img[: self._count]
        assert weight_per_img.shape[0] == len(repeated_params) == self._count

        splits = min(self._cpu_count, self._count)
        params_per_proc = np.array_split(repeated_params, splits)
        weights_per_proc = np.array_split(weight_per_img, splits)

        generation_params = []
        offset = 0
        for i, (param, w) in enumerate(zip(params_per_proc, weights_per_proc)):
            indices = list(range(offset, offset + len(param)))
            offset += len(param)
            generation_params.append((param, w, indices))

        if use_multiprocessing:
            with Pool(processes=self._cpu_count) as pool:
                pool.starmap(self._generate_image_batch, generation_params)
        else:
            for i, param in enumerate(generation_params):
                self._generate_image_batch(*param)

    def _generate_image_batch(
        self, params: np.ndarray, weights: np.ndarray, indices: List[int]
    ) -> None:
        proto = osp.join(self._model_dir, self._MODEL_PROTO_NAME)
        model = osp.join(self._model_dir, self._MODEL_CONFIG_NAME)
        npy = osp.join(self._model_dir, self._HULL_PTS_FILE_NAME)
        pts_in_hull = np.load(npy).transpose().reshape(2, 313, 1, 1).astype(np.float32)

        with open_text(__package__, self._COLORS_FILE) as f:
            background_colors = np.loadtxt(f)

        net = cv.dnn.readNetFromCaffe(proto, model)
        net.getLayer(net.getLayerId("class8_ab")).blobs = [pts_in_hull]
        net.getLayer(net.getLayerId("conv8_313_rh")).blobs = [np.full([1, 313], 2.606, np.float32)]

        for i, param, w in zip(indices, params, weights):
            image = self._generate_image(
                Random(i),
                param,
                self._iterations,
                self._height,
                self._width,
                draw_point=False,
                weight=w,
            )
            color_image = colorize(image, net)
            aug_image = augment(Random(i), color_image, background_colors)
            save_image(
                osp.join(self._output_dir, "{:06d}.png".format(i)), aug_image, create_dir=True
            )

    def _generate_image(
        self,
        rng: Random,
        params: np.ndarray,
        iterations: int,
        height: int,
        width: int,
        draw_point: bool = True,
        weight: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        ifs_function = IFSFunction(rng, prev_x=0.0, prev_y=0.0)
        for param in params:
            ifs_function.add_param(
                param[: ifs_function.NUM_PARAMS], param[ifs_function.NUM_PARAMS], weight
            )
        ifs_function.calculate(iterations)
        img = ifs_function.draw(height, width, draw_point)
        return img

    def _generate_category(self, rng: Random, base_h: int = 512, base_w: int = 512) -> np.ndarray:
        pixels = -1
        i = 0
        while pixels < self._threshold and i < self._iterations:
            param_size = rng.randint(2, 7)
            params = np.zeros((param_size, IFSFunction.NUM_PARAMS + 1), dtype=np.float32)

            sum_proba = 1e-5
            for p_idx in range(param_size):
                a, b, c, d, e, f = [rng.uniform(-1.0, 1.0) for _ in range(IFSFunction.NUM_PARAMS)]
                prob = abs(a * d - b * c)
                sum_proba += prob
                params[p_idx] = a, b, c, d, e, f, prob
            params[:, IFSFunction.NUM_PARAMS] /= sum_proba

            fractal_img = self._generate_image(rng, params, self._num_of_points, base_h, base_w)
            pixels = np.count_nonzero(fractal_img) / (base_h * base_w)
            i += 1
        return params

    def _initialize_params(self) -> None:
        if self._count < self._weights.shape[0]:
            self._weights = self._weights[: self._count, :]

        instances_categories = np.ceil(self._count / self._weights.shape[0])
        self._instances = np.ceil(np.sqrt(instances_categories)).astype(int)
        self._categories = np.ceil(instances_categories / self._instances).astype(int)

    @staticmethod
    def _create_weights(num_params):
        # weights from https://openaccess.thecvf.com/content/ACCV2020/papers/Kataoka_Pre-training_without_Natural_Images_ACCV_2020_paper.pdf
        BASE_WEIGHTS = np.ones((num_params,))
        WEIGHT_INTERVAL = 0.4
        INTERVAL_MULTIPLIERS = (-2, -1, 1, 2)
        weight_vectors = [BASE_WEIGHTS]

        for weight_index in range(num_params):
            for multiplier in INTERVAL_MULTIPLIERS:
                modified_weights = BASE_WEIGHTS.copy()
                modified_weights[weight_index] += multiplier * WEIGHT_INTERVAL
                weight_vectors.append(modified_weights)
        weights = np.array(weight_vectors)
        return weights

    @classmethod
    def _download_colorization_model(cls, path: str) -> None:
        proto_file_name = cls._MODEL_PROTO_NAME
        config_file_name = cls._MODEL_CONFIG_NAME
        hull_file_name = cls._HULL_PTS_FILE_NAME
        proto_path = osp.join(path, proto_file_name)
        config_path = osp.join(path, config_file_name)
        hull_path = osp.join(path, hull_file_name)

        if not (
            osp.exists(proto_path) and osp.exists(config_path) and osp.exists(hull_path)
        ) and not os.access(path, os.W_OK):
            raise ValueError(
                "Please provide a path to a colorization model or "
                "a path to a writable directory to download the model"
            )

        if not osp.exists(proto_path):
            log.info(
                "Downloading the '%s' file for image colorization model to '%s'",
                proto_file_name,
                path,
            )
            url = "https://raw.githubusercontent.com/richzhang/colorization/caffe/colorization/models/"
            data = requests.get(url + proto_file_name)
            with open(proto_path, "wb") as f:
                f.write(data.content)

        if not osp.exists(config_path):
            log.info(
                "Downloading the '%s' file config for image colorization model to '%s'",
                config_file_name,
                path,
            )
            url = "http://eecs.berkeley.edu/~rich.zhang/projects/2016_colorization/files/demo_v2/"
            data = requests.get(url + config_file_name)
            with open(config_path, "wb") as f:
                f.write(data.content)

        if not osp.exists(hull_path):
            log.info(
                "Downloading the '%s' file for image colorization to '%s'",
                hull_file_name,
                path,
            )
            url = "https://github.com/richzhang/colorization/raw/caffe/colorization/resources/"
            data = requests.get(url + hull_file_name)
            with open(hull_path, "wb") as f:
                f.write(data.content)
