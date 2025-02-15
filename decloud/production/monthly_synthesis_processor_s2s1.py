#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copyright (c) 2020-2022 INRAE

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""
"""Process time series with Meraner-like models"""
import argparse
import datetime
import os
import sys
import logging

import otbApplication

from decloud.core import system
from decloud.preprocessing.constants import padded_tensor_name
from decloud.production.products import Factory as ProductsFactory
import pyotb


def monthly_synthesis_inference(
    sources, 
    sources_scales, 
    pad, 
    ts, 
    savedmodel_dir, 
    out_tensor, 
    out_nodatavalue,
    out_pixeltype, 
    nodatavalues=None
):
    """
    Uses OTBTF TensorflowModelServe for the inference, perform some post-processing to keep only valid pixels.

    :param sources: a dict of sources, with keys=placeholder name, and value=str/otbimage
    :param sources_scales: a dict of sources scales (1=unit)
    :param pad: Margin size for blocking artefacts removal
    :param ts: Tile size. Tune this to process larger output image chunks, and speed up the process.
    :param savedmodel_dir: SavedModel directory
    :param out_tensor: output tensor name
    :param out_nodatavalue: NoData value for the output reconstructed S2t image
    :param out_pixeltype: PixelType for the output reconstructed S2t image
    :param nodatavalues: Optional, dictionary of NoData with keys=placeholder name
    :param with_20m_bands: Whether to compute the 20m bands. Default False
    """
    if nodatavalues is None:
        nodatavalues = {
            "s1_t0": 0, 
            "s1_t1": 0, 
            "s1_t2": 0, 
            "s1_t3": 0, 
            "s1_t4": 0, 
            "s1_t5": 0, 
            "s2_t0": -10000,
            "s2_t1": -10000, 
            "s2_t2": -10000,
            "s2_t3": -10000, 
            "s2_t4": -10000, 
            "s2_t5": -10000
        }

    logging.info("Setup inference pipeline")
    logging.info("Input sources: %s", sources)
    logging.info("Input pad: %s", pad)
    logging.info("Input tile size: %s", ts)
    logging.info("SavedModel directory: %s", savedmodel_dir)
    logging.info("Output tensor name: %s", out_tensor)

    # Receptive/expression fields
    gen_fcn = pad
    efield = ts  # Expression field
    if efield % 64 != 0:
        logging.fatal("Please chose a tile size that is a multiple of 64")
        quit()
    rfield = int(efield + 2 * gen_fcn)  # Receptive field
    logging.info("Receptive field: %s, Expression field: %s", rfield, efield)

    # Setup TensorFlowModelServe
    system.set_env_var("OTB_TF_NSOURCES", str(len(sources)))
    infer_params = {}

    # Setup BandMath for post processing
    bm_params = {}
    mask_expr = "0"

    # Inputs
    k = 0  # counter used for the im# of postprocessing mask
    for i, (placeholder, source) in enumerate(sources.items()):
        logging.info("Preparing source %s for placeholder %s", i + 1, placeholder)

        def get_key(key):
            """ Return the parameter key for the current source """
            return "source{}.{}".format(i + 1, key)

        src_rfield = rfield
        if placeholder in sources_scales:
            src_rfield = int(rfield / sources_scales[placeholder])

        infer_params.update({get_key("il"): [source]})

        # Update post processing BandMath expression
        if placeholder != 'dem' and '20m' not in placeholder:
            nodatavalue = nodatavalues[placeholder]
            n_channels = pyotb.get_nbchannels(source)
            mask_expr += "||"
            mask_expr += "&&".join(["im{}b{}=={}".format(k + 1, b, nodatavalue) for b in range(1, 1 + n_channels)])
            bm_params.update({'il': [source]})
            k += 1

        infer_params.update({
            get_key("rfieldx"): src_rfield,
            get_key("rfieldy"): src_rfield,
            get_key("placeholder"): placeholder
        })

    # Model
    infer_params.update({
        "model.dir": savedmodel_dir, 
        "model.fullyconv": True,
        "output.names": [padded_tensor_name(out_tensor, pad)],
        "output.efieldx": efield, 
        "output.efieldy": efield,
        "optim.tilesizex": efield, 
        "optim.tilesizey": efield,
        "optim.disabletiling": True
    })
    infer = pyotb.TensorflowModelServe(infer_params)

    # For ESA Sentinel-2, remove potential zeros the network may have introduced in the valid parts of the image
    if out_pixeltype == otbApplication.ImagePixelType_uint16:
        n_channels = pyotb.get_nbchannels(infer.out)
        exp = ';'.join([f'(im1b{b}<=1 ? 1 : im1b{b})' for b in range(1, 1 + n_channels)])
        rmzeros = pyotb.App("BandMathX", il=[infer.out], exp=exp)
        rmzeros.SetParameterOutputImagePixelType("out", out_pixeltype)
    else:
        rmzeros = infer

    # Mask for post processing
    mask_expr += "?0:255"
    bm_params.update({'exp': mask_expr})
    bm = pyotb.BandMath(bm_params)

    # Closing post processing mask to remove small groups of NoData pixels
    closing = pyotb.BinaryMorphologicalOperation(
        bm, 
        filter="closing", 
        foreval=255, 
        structype="box",
        xradius=5, 
        yradius=5
    )

    # Erode post processing mask
    erode = pyotb.BinaryMorphologicalOperation(
        closing, 
        filter="erode", 
        foreval=255, 
        structype="box",
        xradius=pad, 
        yradius=pad
    )

    # Superimpose the eroded post processing mask
    resample = pyotb.Superimpose(
        inm=erode, 
        interpolator="nn", 
        lms=192, 
        inr=infer
    )

    # Apply nodata where the post processing mask is "0"
    mnodata = pyotb.ManageNoData({
        "in": rmzeros, 
        "mode": "apply", 
        "mode.apply.mask": resample,
        "mode.apply.ndval": out_nodatavalue
    })

    mnodata.SetParameterOutputImagePixelType("out", out_pixeltype)
    return mnodata


if __name__ == "__main__":
    # Logger
    system.basic_logging_init()

    # Parser
    parser = argparse.ArgumentParser(
        description="Remove clouds in a time series of Sentinel-2 image, using joint optical and SAR images.")

    # Input images
    parser.add_argument("--il_s2", nargs='+', help="List of Sentinel-2 images, can be a list of paths or "
                                                   "a .txt file containing paths")
    parser.add_argument("--il_s1", nargs='+', help="List of Sentinel-1 images, can be a list of paths or "
                                                   "a .txt file containing paths")
    parser.add_argument("--s2_dir", help="Directory of Sentinel-2 images. Enables to treat all the images of "
                                         "a directory. Used only if il_s2 is not specified")
    parser.add_argument("--s1_dir", help="Directory of Sentinel-1 images. Enables to treat all the images of "
                                         "a directory. Used only if il_s1 is not specified")
    parser.add_argument("--dem", help="DEM path")
    parser.add_argument("--out_dir", required=True, help="Output directory for the monthly synthesis")
    parser.add_argument("--model", required=True, help="Path to the saved model directory, containing saved_model.pb")
    parser.add_argument("--ulx", help="Upper Left X of the ROI, in geographic coordinates. Optional", type=float)
    parser.add_argument("--uly", help="Upper Left Y of the ROI, in geographic coordinates. Optional", type=float)
    parser.add_argument("--lrx", help="Lower Right X of the ROI, in geographic coordinates. Optional", type=float)
    parser.add_argument("--lry", help="Lower Right Y of the ROI, in geographic coordinates. Optional", type=float)
    parser.add_argument("--year", help="Starting date, format YYYY-MM-DD. Optional")
    parser.add_argument("--month", help="End date, format YYYY-MM-DD. Optional")
    parser.add_argument('--ts', default=256, type=int,
                        help="Tile size. Tune this to process larger output image chunks, and speed up the process.")
    parser.add_argument('--overwrite', dest='overwrite', action='store_true',
                        help="Whether to overwrite results if already exist")
    parser.set_defaults(overwrite=False)
    parser.add_argument('--write_intermediate', dest='write_intermediate', action='store_true',
                        help="Whether to write S1t & S2t input rasters used by the model.")
    parser.set_defaults(write_intermediate=False)

    if len(sys.argv) == 1:
        parser.print_help()
        parser.exit()

    params = parser.parse_args()

    if not (params.il_s2 or params.s2_dir):
        raise Exception('Missing --il_s2 or --s2_dir argument')
    if not (params.il_s1 or params.s1_dir):
        raise Exception('Missing --il_s1 or --s1_dir argument')

    if params.il_s2 and params.s2_dir:
        logging.warning('Both --il_s2 and --s2_dir were specified. Discarding --s2_dir')
        params.s2_dir = None
    if params.il_s1 and params.s1_dir:
        logging.warning('Both --il_s1 and --s1_dir were specified. Discarding --s1_dir')
        params.s1_dir = None

    # Getting all the S2 filepaths
    if params.s2_dir:
        s2_image_paths = [os.path.join(params.s2_dir, name) for name in os.listdir(params.s2_dir)]
    elif params.il_s2[0].endswith('.txt'):
        with open(params.il_s2[0], 'r') as f:
            s2_image_paths = [x.strip() for x in f.readlines()]
    else:
        s2_image_paths = params.il_s2

    # Getting all the S1 filepaths
    if params.s1_dir:
        s1_image_paths = [os.path.join(params.s1_dir, name) for name in os.listdir(params.s1_dir)]
    elif params.il_s1[0].endswith('.txt'):
        with open(params.il_s1[0], 'r') as f:
            s1_image_paths = [x.strip() for x in f.readlines()]
    else:
        s1_image_paths = params.il_s1

    # Converting filepaths to S2 products
    input_s2_products = {}
    product_count, invalid_count = 0, 0
    for product_path in s2_image_paths:
        product = ProductsFactory.create(product_path, 's2', verbose=False)
        if product:
            input_s2_products[product_path] = product
            product_count += 1
        else:
            invalid_count += 1
    logging.info('Retrieved {} S2 products from disk. '
                 'Discarded {} paths that were not S2 products'.format(product_count, invalid_count))

    # Converting filepaths to S1 products
    input_s1_products = []
    product_count, invalid_count = 0, 0
    for product_path in s1_image_paths:
        product = ProductsFactory.create(product_path, 's1', verbose=False)
        if product:
            input_s1_products.append(product)
            product_count += 1
        else:
            invalid_count += 1
    logging.info('Retrieved {} S1 products from disk. '
                 'Discarded {} paths that were not S1 products'.format(product_count, invalid_count))

    if not system.is_dir(params.out_dir):
        system.mkdir(params.out_dir)
    output_path = os.path.join(params.out_dir, 'monthly_synthesis_s2s1_{}{}.tif'.format(params.year, params.month))

    # ==================
    # Input parameters
    # ==================
    s1_Nimages = 6  # number of images to choose for s1t
    central_date = datetime.datetime(int(params.year), int(params.month), 15)
    delta_days = 22
    model_nb_images = 6

    # looping through the files
    candidates = []
    for s2_filepath, s2_product in input_s2_products.items():
        if (abs(s2_product.get_date() - central_date) < datetime.timedelta(days=delta_days) and
                s2_product.get_nodata_percentage() < 0.05):
            # Choosing S1t
            def _closest_date(x):
                """Helper to sort S1 products
                :param x: input product
                """
                return abs(s2_product.get_timestamp() - x.get_timestamp())


            input_s1_products.sort(key=_closest_date, reverse=True)
            input_s1_images_10m = [product.get_raster_10m() for product in input_s1_products]
            # creating a mosaic with the N closest S1 images
            s1t = pyotb.App('Mosaic', input_s1_images_10m[-s1_Nimages:], nodata=0)

            candidates.append({'s2': s2_product, 's1': s1t})

    # If too many images, keeping only the 6 best images for synthesis
    if len(candidates) > model_nb_images:
        candidates.sort(key=lambda x: x['s2'].get_cloud_percentage())
        candidates = candidates[:model_nb_images]

    # Duplicating if not enough images
    if len(candidates) < model_nb_images:
        candidates.sort(key=lambda x: x['s2'].get_cloud_percentage())
        candidates = [candidates[0]] * (model_nb_images - len(candidates)) + candidates

    # Gathering as a dictionary
    sources = {}
    for i, candidate in enumerate(candidates):
        sources.update({'s2_t{}'.format(i): candidate['s2'].get_raster_10m(),
                        's1_t{}'.format(i): candidate['s1']})

    # Sources scales
    sources_scales = {}

    if params.dem is not None:
        sources.update({"dem": params.dem})
        sources_scales.update({"dem": 2})

    # Inference
    out_tensor = "s2_estim"
    processor = monthly_synthesis_inference(
        sources=sources, 
        sources_scales=sources_scales, 
        pad=64,
        ts=params.ts, 
        savedmodel_dir=params.model,
        out_tensor=out_tensor,
        out_nodatavalue=-10000,
        out_pixeltype=otbApplication.ImagePixelType_int16
    )

    # If needed, extracting ROI of the reconstructed image
    if params.lrx and params.lry and params.ulx and params.uly:
        processor = pyotb.ExtractROI({
            'in': processor, 
            'mode': 'extent', 
            'mode.extent.unit': 'phy',
            'mode.extent.ulx': params.ulx, 
            'mode.extent.uly': params.uly,
            'mode.extent.lrx': params.lrx, 
            'mode.extent.lry': params.lry
        })

    # OTB extended filename that will be used for all writing
    ext_fname = (
        "&streaming:type=tiled"
        "&streaming:sizemode=height"
        f"&streaming:sizevalue={params.ts}"
        "&gdal:co:COMPRESS=DEFLATE"
        "&gdal:co:TILED=YES"
    )

    processor.write(out=output_path, filename_extension=ext_fname)

    # Writing the inputs sources of the model
    if params.write_intermediate:
        for name, source in sources.items():
            if name != 'dem':
                # If needed, extracting ROI of every rasters
                if params.lrx and params.lry and params.ulx and params.uly:
                    source = pyotb.ExtractROI({
                        'in': source, 
                        'mode': 'extent', 
                        'mode.extent.unit': 'phy',
                        'mode.extent.ulx': params.ulx, 
                        'mode.extent.uly': params.uly,
                        'mode.extent.lrx': params.lrx, 
                        'mode.extent.lry': params.lry
                    })
                pyotb.Input(source).write(
                    os.path.join(
                        os.path.dirname(output_path),
                        os.path.basename(output_path).replace('monthly_synthesis', name)
                    ),
                    pixel_type='int16', 
                    filename_extension=ext_fname
                )
