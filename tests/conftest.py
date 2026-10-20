"""Shared fixtures; the images themselves are built in images.py."""

import pytest
from images import SAMPLES
from PIL import Image


@pytest.fixture
def rgb_gradient() -> Image.Image:
    return SAMPLES["gradient"]()


@pytest.fixture
def rgb_noise() -> Image.Image:
    return SAMPLES["noise"]()


@pytest.fixture
def rgb_one_pixel() -> Image.Image:
    return SAMPLES["one-pixel"]()


@pytest.fixture
def rgb_many_colors() -> Image.Image:
    return SAMPLES["many-colors"]()


@pytest.fixture
def p_transparent() -> Image.Image:
    return SAMPLES["palette-transparent"]()


@pytest.fixture
def gradient_png(tmp_path, rgb_gradient):
    """The gradient image saved as tmp_path/in.png."""
    path = tmp_path / "in.png"
    rgb_gradient.save(path)
    return path
