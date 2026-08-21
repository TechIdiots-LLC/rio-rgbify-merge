from io import BytesIO
from PIL import Image
import numpy as np
import rasterio
from rasterio._io import virtual_file_to_buffer
from enum import Enum
import logging

def _custom_factors(factors):
    """Read the four numbers a custom encoding is unreadable without.

    All or nothing: three of four is no better than none, since the tile
    cannot be decoded either way, and a partial guess would produce heights
    that look plausible and are wrong.
    """
    if not factors:
        raise ValueError(
            "encoding 'custom' needs redFactor, greenFactor, blueFactor and baseShift"
        )
    try:
        values = (
            float(factors["redFactor"]),
            float(factors["greenFactor"]),
            float(factors["blueFactor"]),
            float(factors["baseShift"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "encoding 'custom' needs redFactor, greenFactor, blueFactor and baseShift"
        ) from error
    if any(v == 0 for v in values[:3]):
        raise ValueError("custom channel factors must not be zero")
    return values


class ImageFormat(Enum):
    PNG = "png"
    WEBP = "webp"

class ImageEncoder:

    @staticmethod
    def data_to_rgb(data, encoding, interval, base_val=-10000, round_digits=0, factors=None):
        """
        Given an arbitrary (rows x cols) ndarray,
        encode the data into uint8 RGB from an arbitrary
        base and interval

        Parameters
        ----------
        data: ndarray
            (rows x cols) ndarray of data to encode
        encoding: str
            output tile encoding (mapbox or terrarium)
        interval: float
            the interval at which to encode
        base_val: float
            the base value to apply when using mapbox. Default is -10000
        round_digits: int
            erased less significant digits

        Returns
        --------
        ndarray: rgb data
            a uint8 (3 x rows x cols) ndarray with the data encoded
        """
        logging.debug(f"data_to_rgb called with shape: {data.shape}, encoding: {encoding}, interval: {interval}, base_val: {base_val}, round_digits: {round_digits}")
        if not isinstance(data, np.ndarray):
            raise ValueError("Input data must be a numpy array")

        data = data.astype(np.float64)
        if(encoding == "custom"):
            # MapLibre's own packing: scale by the smallest factor so the
            # least-significant channel is not rounded away before the others
            # have had their share.
            red, green, blue, shift = _custom_factors(factors)
            min_scale = min(red, green, blue)
            ceiling = round((256 * 256 * 256 - 1) * (blue / min_scale))
            scaled = np.clip(np.round((data + shift) / min_scale), 0, ceiling)
            rows, cols = data.shape
            rgb = np.zeros((3, rows, cols), dtype=np.uint8)
            rgb[0] = np.floor(scaled * min_scale / red) % 256
            rgb[1] = np.floor(scaled * min_scale / green) % 256
            rgb[2] = np.floor(scaled * min_scale / blue) % 256
            return rgb
        if(encoding == "terrarium"):
            data = np.clip(data, -32768, 32767)
            data += 32768
        else:
            # CLAMP values before encoding for Mapbox encoding
            data = np.clip(data, base_val, 100000)
            data -= base_val   # Apply offset
            data /= interval
            
        data = np.around(data / 2**round_digits) * 2**round_digits

        rows, cols = data.shape
        rgb = np.zeros((3, rows, cols), dtype=np.uint8)
        if(encoding == "terrarium"):
            rgb[0] = np.floor(data // 256)
            rgb[1] = np.floor(data % 256)
            rgb[2] = np.floor((data - np.floor(data)) * 256)
        else:
            rgb[0] = np.floor((data / (256 * 256)) % 256).astype(np.uint8)
            rgb[1] = np.floor((data / 256) % 256).astype(np.uint8)
            rgb[2] = np.floor(data % 256).astype(np.uint8)
        return rgb
    
    @staticmethod
    def _custom_factors_or_raise(factors):
        """Raise unless the four custom-encoding numbers are all present."""
        return _custom_factors(factors)

    @staticmethod
    def _decode(data: np.ndarray, base: float, interval: float, encoding: str, factors: dict = None) -> np.ndarray:
        """
        Utility to decode RGB encoded data

        Parameters
        ----------
        data: np.ndarray
            RGB data to decode
        base: float
            Base value for mapbox encoding
        interval: float
            Interval value for mapbox encoding
        encoding: str
            Encoding type ('terrarium' or 'mapbox')

        Returns
        -------
        np.ndarray
            Decoded elevation data
        """
        data = data.astype(np.float64)
        if(encoding == "custom"):
            # MapLibre's style-spec formula, with the factors supplied rather
            # than assumed. baseShift is subtracted, which is the opposite sign
            # to a mapbox base_val: -10000 there is a base_shift of 10000 here.
            red, green, blue, shift = _custom_factors(factors)
            return (data[0] * red + data[1] * green + data[2] * blue) - shift
        if(encoding == "terrarium"):
            return (data[0] * 256 + data[1] + data[2] / 256) - 32768
        else:
            return base + (((data[0] * 256 * 256) + (data[1] * 256) + data[2]) * interval)

    @staticmethod
    def _mask_elevation(elevation: np.ndarray, mask_values: list = [0.0]) -> np.ndarray:
        """
        Mask specific elevation values with NaN

        Parameters
        ----------
        elevation: np.ndarray
            Elevation data array
        mask_values: list
            List of values to mask with NaN. Default is [0.0]

        Returns
        -------
        np.ndarray
            Masked elevation array
        """
        mask = np.zeros_like(elevation, dtype=bool)
        for mask_value in mask_values:
            mask = np.logical_or(mask, elevation == mask_value)
        return np.where(mask, np.nan, elevation)
    
    @staticmethod
    def _range_check(datarange):
        """
        Utility to check if data range is outside of precision for 3 digit base 256
        """
        maxrange = 256 ** 3
        return datarange > maxrange

    @staticmethod
    def save_rgb_to_bytes(rgb_data: np.ndarray, output_image_format: str | ImageFormat, default_tile_size: int = 512) -> bytes:
        print(f"save_rgb_to_bytes called with rgb data shape {rgb_data.shape}")
        print(f"Requested format: {output_image_format}, type: {type(output_image_format)}")
        
        # Convert string to enum if needed
        if isinstance(output_image_format, str):
            try:
                output_image_format = ImageFormat(output_image_format.lower())
            except ValueError:
                print(f"Invalid format {output_image_format}, falling back to PNG")
                output_image_format = ImageFormat.PNG
        
        print(f"Using format: {output_image_format}")
        
        try:
            # Create image
            if rgb_data.ndim == 3:
                moved_data = np.moveaxis(rgb_data, 0, -1).astype(np.uint8)
                print(f"Moved data shape: {moved_data.shape}, dtype: {moved_data.dtype}")
                image = Image.fromarray(moved_data, 'RGB')
            elif rgb_data.ndim == 4:
                moved_data = np.moveaxis(rgb_data, 0, -1).astype(np.uint8)
                image = Image.fromarray(moved_data, 'RGBA')
            else:
                tile_size = default_tile_size
                image = Image.fromarray(np.moveaxis(np.zeros((3,tile_size,tile_size), dtype=np.uint8), 0, -1), 'RGB')
            
            print(f"Image created - size: {image.size}, mode: {image.mode}")
            
            with BytesIO() as f:
                if output_image_format == ImageFormat.WEBP:
                    print("Attempting to save as WebP")
                    image.save(f, format='WEBP', lossless=True)
                else:
                    print("Attempting to save as PNG")
                    image.save(f, format='PNG')
                
                f.seek(0)
                image_bytes = f.getvalue()
                print(f"Buffer size after save: {len(image_bytes)}")
                
                return bytes(image_bytes)
                
        except Exception as e:
            print(f"Failed to encode image: {str(e)}")
            raise