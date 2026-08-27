"""minjpg - batch generation of web-sized ``*_min.jpg`` files with MozJPEG.

Reproduces the Squoosh workflow (MozJPEG, YCbCr, ImageMagick quantization
table, smoothing, auto chroma subsample) while automatically searching for the
highest quality that keeps every output under a byte budget.
"""

__version__ = "1.0.0"
