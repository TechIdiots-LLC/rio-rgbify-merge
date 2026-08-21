"""rio-rgbify: setup."""

import os

from setuptools import setup, find_packages

# Parse the version from the package. The release workflow reads this same
# assignment to decide the tag, so the two cannot disagree.
with open("rio_rgbify/__init__.py") as f:
    for line in f:
        if line.find("__version__") >= 0:
            version = line.split("=")[1].strip()
            version = version.strip('"')
            version = version.strip("'")
            break

with open(os.path.join(os.path.dirname(__file__), "README.md"), encoding="utf-8") as f:
    long_description = f.read()

# Runtime requirements.
#
# numpy and psutil are imported directly by the package (mbtiler, merger,
# image) and were missing here, so an install from a clean environment failed
# at import. rio-mucho is gone: nothing imports it any more.
inst_reqs = [
    "click",
    "rasterio~=1.0",
    "numpy",
    "Pillow",
    "mercantile",
    "scipy",
    "psutil",
]

extra_reqs = {
    "test": ["pytest", "pytest-cov", "codecov", "hypothesis", "raster_tester"],
    "dev": [
        "pytest", "pytest-cov", "codecov", "hypothesis", "raster_tester", "pre-commit"
    ],
}

# The distribution name is not "rio-rgbify": that belongs to mapbox on PyPI,
# last published in 2022. PyPI project names are one flat global namespace --
# an organisation account owns projects but does not scope their names, so
# there is no techidiots-llc/rio-rgbify to publish under.
#
# The import name is unaffected. This installs as rio-rgbify-merge and is
# still `import rio_rgbify`, still registering `rio rgbify` and `rio merge`.
setup(name="rio-rgbify-merge",
      version=version,
      description=u"Encode rasters as Terrain RGB, and merge several terrain sources into one tileset",
      long_description=long_description,
      long_description_content_type="text/markdown",
      classifiers=[
          "Development Status :: 4 - Beta",
          "Intended Audience :: Science/Research",
          "License :: OSI Approved :: MIT License",
          "Programming Language :: Python :: 3",
          "Programming Language :: Python :: 3.10",
          "Programming Language :: Python :: 3.11",
          "Programming Language :: Python :: 3.12",
          "Topic :: Scientific/Engineering :: GIS",
      ],
      keywords="raster gis terrain elevation dem mbtiles rasterio",
      author=u"Damon Burgett",
      author_email="damon@mapbox.com",
      maintainer=u"TechIdiots-LLC",
      url="https://github.com/TechIdiots-LLC/rio-rgbify-merge",
      project_urls={
          "Changelog": "https://github.com/TechIdiots-LLC/rio-rgbify-merge/blob/main/CHANGELOG.md",
          "Source": "https://github.com/TechIdiots-LLC/rio-rgbify-merge",
          "Upstream": "https://github.com/mapbox/rio-rgbify",
      },
      # The LICENSE file is MIT (Copyright 2016 Mapbox). setup.py said BSD,
      # which upstream also does -- wrong either way, and PyPI would have
      # published the mismatch.
      license="MIT",
      packages=find_packages(exclude=["ez_setup", "examples", "tests", "test"]),
      include_package_data=True,
      zip_safe=False,
      python_requires=">=3.10",
      install_requires=inst_reqs,
      extras_require=extra_reqs,
      entry_points="""
      [rasterio.rio_plugins]
      rgbify=rio_rgbify.scripts.cli:rgbify
      merge=rio_rgbify.scripts.cli:merge
      """
     )
