"""Shared fixtures; the images themselves are built in images.py."""

import pytest
from images import gradient, many_colors, noise, palette_with_transparency
from PIL import Image


@pytest.fixture
def rgb_gradient() -> Image.Image:
    return gradient(64, 48)


@pytest.fixture
def rgb_noise() -> Image.Image:
    return noise(50, 40, 300)


@pytest.fixture
def rgb_solid() -> Image.Image:
    return Image.new("RGB", (10, 7), (12, 34, 56))


@pytest.fixture
def rgb_one_pixel() -> Image.Image:
    return Image.new("RGB", (1, 1), (1, 2, 3))


@pytest.fixture
def rgb_many_colors() -> Image.Image:
    return many_colors()


@pytest.fixture
def p_transparent() -> Image.Image:
    return palette_with_transparency()
