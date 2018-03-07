# -*- coding: utf-8 -*-

import os
import time
import warnings
from copy import copy
from typing import Iterable

# custom
try:
    import gdal
except ImportError:
    from osgeo import gdal
import numpy as np

try:
    import pyfftw
except ImportError:
    pyfftw = None
from shapely.geometry import Point, Polygon
from skimage.exposure import rescale_intensity

# internal modules
from .DeShifter import DESHIFTER, _dict_rspAlg_rsp_Int
from . import geometry as GEO
from . import plotting as PLT

from geoarray import GeoArray
from py_tools_ds.convenience.object_oriented import alias_property
from py_tools_ds.geo.coord_calc import get_corner_coordinates
from py_tools_ds.geo.vector.topology import get_overlap_polygon, get_smallest_boxImYX_that_contains_boxMapYX
from py_tools_ds.geo.projection import prj_equal, get_proj4info
from py_tools_ds.geo.vector.geometry import boxObj, round_shapelyPoly_coords
from py_tools_ds.geo.coord_grid import move_shapelyPoly_to_image_grid
from py_tools_ds.geo.coord_trafo import reproject_shapelyGeometry, mapXY2imXY, imXY2mapXY
from py_tools_ds.geo.raster.reproject import warp_ndarray
from py_tools_ds.geo.map_info import geotransform2mapinfo
from py_tools_ds.similarity.raster import calc_ssim
from py_tools_ds.io.vector.writer import write_shp

__author__ = 'Daniel Scheffler'


class GeoArray_CoReg(GeoArray):
    def __init__(self, CoReg_params, imID):
        # type: (dict, str) -> None

        assert imID in ['ref', 'shift']

        # run GeoArray init
        path_or_geoArr = CoReg_params['im_ref'] if imID == 'ref' else CoReg_params['im_tgt']
        nodata = CoReg_params['nodata'][0 if imID == 'ref' else 1]
        progress = CoReg_params['progress']
        q = CoReg_params['q'] if not CoReg_params['v'] else False

        super(GeoArray_CoReg, self).__init__(path_or_geoArr, nodata=nodata, progress=progress, q=q)

        self.imID = imID
        self.imName = 'reference image' if imID == 'ref' else 'image to be shifted'
        self.v = CoReg_params['v']

        assert isinstance(self, GeoArray), \
            'Something went wrong with the creation of GeoArray instance for the %s. The created ' \
            'instance does not seem to belong to the GeoArray class. If you are working in Jupyter Notebook, reset ' \
            'the kernel and try again.' % self.imName

        # set title to be used in plots
        self.title = os.path.basename(self.filePath) if self.filePath else self.imName

        # validate params
        # assert self.prj, 'The %s has no projection.' % self.imName # TODO
        # assert not re.search('LOCAL_CS', self.prj), 'The %s is not georeferenced.' % self.imName # TODO
        assert self.gt, 'The %s has no map information.' % self.imName

        # set band4match
        self.band4match = (CoReg_params['r_b4match'] if imID == 'ref' else CoReg_params['s_b4match']) - 1
        assert self.bands >= self.band4match + 1 >= 1, \
            "The %s has %s %s. So its band number to match must be %s%s. Got %s." \
            % (self.imName, self.bands, 'bands' if self.bands > 1 else
               'band', 'between 1 and ' if self.bands > 1 else '', self.bands, self.band4match)

        # set footprint_poly
        given_footprint_poly = CoReg_params['footprint_poly_%s' % ('ref' if imID == 'ref' else 'tgt')]
        given_corner_coord = CoReg_params['data_corners_%s' % ('ref' if imID == 'ref' else 'tgt')]

        if given_footprint_poly:
            self.footprint_poly = given_footprint_poly
        elif given_corner_coord is not None:
            self.footprint_poly = Polygon(given_corner_coord)
        elif not CoReg_params['calc_corners']:
            # use the image extent
            self.footprint_poly = Polygon(get_corner_coordinates(gt=self.gt, cols=self.cols, rows=self.rows))
        else:
            # footprint_poly is calculated automatically by GeoArray
            if not CoReg_params['q']:
                print('Calculating actual data corner coordinates for %s...' % self.imName)
            self.calc_mask_nodata(fromBand=self.band4match)  # this avoids that all bands have to be read

        # validate footprint poly
        if not self.footprint_poly.is_valid:
            self.footprint_poly = self.footprint_poly.buffer(0)

        if not self.q:
            print('Bounding box of calculated footprint for %s:\n\t%s' % (self.imName, self.footprint_poly.bounds))

        # add bad data mask
        given_mask = CoReg_params['mask_baddata_%s' % ('ref' if imID == 'ref' else 'tgt')]
        if given_mask:
            self.mask_baddata = given_mask  # runs GeoArray.mask_baddata.setter -> sets it to BadDataMask()

    poly = alias_property('footprint_poly')  # ensures that self.poly is updated if self.footprint_poly is updated


class COREG(object):
    """See help(COREG) for documentation!"""

    def __init__(self, im_ref, im_tgt, path_out=None, fmt_out='ENVI', out_crea_options=None, r_b4match=1, s_b4match=1,
                 wp=(None, None), ws=(256, 256), max_iter=5, max_shift=5, align_grids=False, match_gsd=False,
                 out_gsd=None, target_xyGrid=None, resamp_alg_deshift='cubic', resamp_alg_calc='cubic',
                 footprint_poly_ref=None, footprint_poly_tgt=None, data_corners_ref=None, data_corners_tgt=None,
                 nodata=(None, None), calc_corners=True, binary_ws=True, mask_baddata_ref=None, mask_baddata_tgt=None,
                 CPUs=None, force_quadratic_win=True, progress=True, v=False, path_verbose_out=None, q=False,
                 ignore_errors=False):

        """Detects and corrects global X/Y shifts between a target and refernce image. Geometric shifts are calculated
        at a specific (adjustable) image position. Correction performs a global shifting in X- or Y direction.

        :param im_ref(str, GeoArray):   source path (any GDAL compatible image format is supported) or GeoArray instance
                                        of reference image
        :param im_tgt(str, GeoArray):   source path (any GDAL compatible image format is supported) or GeoArray instance
                                        of image to be shifted
        :param path_out(str):           target path of the coregistered image
                                            - if None (default), the method correct_shifts() does not write to disk
                                            - if 'auto': /dir/of/im1/<im1>__shifted_to__<im0>.bsq
        :param fmt_out(str):            raster file format for output file. ignored if path_out is None. can be any GDAL
                                        compatible raster file format (e.g. 'ENVI', 'GeoTIFF'; default: ENVI). Refer to
                                        http://www.gdal.org/formats_list.html to get a full list of supported formats.
        :param out_crea_options(list):  GDAL creation options for the output image,
                                        e.g. ["QUALITY=80", "REVERSIBLE=YES", "WRITE_METADATA=YES"]
        :param r_b4match(int):          band of reference image to be used for matching (starts with 1; default: 1)
        :param s_b4match(int):          band of shift image to be used for matching (starts with 1; default: 1)
        :param wp(tuple):               custom matching window position as map values in the same projection like the
                                        reference image (default: central position of image overlap)
        :param ws(tuple):               custom matching window size [pixels] (default: (256,256))
        :param max_iter(int):           maximum number of iterations for matching (default: 5)
        :param max_shift(int):          maximum shift distance in reference image pixel units (default: 5 px)
        :param align_grids(bool):       align the coordinate grids of the image to be and the reference image
                                        (default: 0)
        :param match_gsd(bool):         match the output pixel size to pixel size of the reference image (default: 0)
        :param out_gsd(tuple):          xgsd ygsd: set the output pixel size in map units
                                        (default: original pixel size of the image to be shifted)
        :param target_xyGrid(list):     a list with a target x-grid and a target y-grid like [[15,45], [15,45]]
                                        This overrides 'out_gsd', 'align_grids' and 'match_gsd'.
        :param resamp_alg_deshift(str)  the resampling algorithm to be used for shift correction (if neccessary)
                                        valid algorithms: nearest, bilinear, cubic, cubic_spline, lanczos, average,
                                                          mode, max, min, med, q1, q3
                                        default: cubic
        :param resamp_alg_calc(str)     the resampling algorithm to be used for all warping processes during calculation
                                        of spatial shifts
                                        (valid algorithms: nearest, bilinear, cubic, cubic_spline, lanczos, average,
                                                           mode, max, min, med, q1, q3)
                                        default: cubic (highly recommended)
        :param footprint_poly_ref(str): footprint polygon of the reference image (WKT string or
                                        shapely.geometry.Polygon),
                                        e.g. 'POLYGON ((299999 6000000, 299999 5890200, 409799 5890200, 409799 6000000,
                                                        299999 6000000))'
        :param footprint_poly_tgt(str): footprint polygon of the image to be shifted (WKT string or
                                        shapely.geometry.Polygon)
                                        e.g. 'POLYGON ((299999 6000000, 299999 5890200, 409799 5890200, 409799 6000000,
                                                        299999 6000000))'
        :param data_corners_ref(list):  map coordinates of data corners within reference image.
                                        ignored if footprint_poly_ref is given.
        :param data_corners_tgt(list):  map coordinates of data corners within image to be shifted.
                                        ignored if footprint_poly_tgt is given.
        :param nodata(tuple):           no data values for reference image and image to be shifted
        :param calc_corners(bool):      calculate true positions of the dataset corners in order to get a useful
                                        matching window position within the actual image overlap
                                        (default: 1; deactivated if '-cor0' and '-cor1' are given
        :param binary_ws(bool):         use binary X/Y dimensions for the matching window (default: 1)
        :param mask_baddata_ref(str, GeoArray): path to a 2D boolean mask file (or an instance of GeoArray) for the
                                                reference image where all bad data pixels (e.g. clouds) are marked with
                                                True and the remaining pixels with False. Must have the same geographic
                                                extent and projection like 'im_ref'. The mask is used to check if the
                                                chosen matching window position is valid in the sense of useful data.
                                                Otherwise this window position is rejected.
        :param mask_baddata_tgt(str, GeoArray): path to a 2D boolean mask file (or an instance of GeoArray) for the
                                                image to be shifted where all bad data pixels (e.g. clouds) are marked
                                                with True and the remaining pixels with False. Must have the same
                                                geographic extent and projection like 'im_ref'. The mask is used to
                                                check if the chosen matching window position is valid in the sense of
                                                useful data. Otherwise this window position is rejected.
        :param CPUs(int):               number of CPUs to use during pixel grid equalization
                                        (default: None, which means 'all CPUs available')
        :param force_quadratic_win(bool):   force a quadratic matching window (default: 1)
        :param progress(bool):          show progress bars (default: True)
        :param v(bool):                 verbose mode (default: False)
        :param path_verbose_out(str):   an optional output directory for intermediate results
                                        (if not given, no intermediate results are written to disk)
        :param q(bool):                 quiet mode (default: False)
        :param ignore_errors(bool):     Useful for batch processing. (default: False)
                                        In case of error COREG.success == False and COREG.x_shift_px/COREG.y_shift_px
                                        is None
        """

        self.params = dict([x for x in locals().items() if x[0] != "self"])

        # assertions
        assert gdal.GetDriverByName(fmt_out), "'%s' is not a supported GDAL driver." % fmt_out
        if match_gsd and out_gsd:
            warnings.warn("'-out_gsd' is ignored because '-match_gsd' is set.\n")
        if out_gsd:
            assert isinstance(out_gsd, list) and len(out_gsd) == 2, 'out_gsd must be a list with two values.'
        if data_corners_ref and not isinstance(data_corners_ref[0],
                                               list):  # group if not [[x,y],[x,y]..] but [x,y,x,y,]
            data_corners_ref = [data_corners_ref[i:i + 2] for i in range(0, len(data_corners_ref), 2)]
        if data_corners_tgt and not isinstance(data_corners_tgt[0], list):  # group if not [[x,y],[x,y]..]
            data_corners_tgt = [data_corners_tgt[i:i + 2] for i in range(0, len(data_corners_tgt), 2)]
        if nodata:
            assert isinstance(nodata, Iterable) and len(nodata) == 2, \
                "'nodata' must be an iterable with two values. Got %s with length %s." % (type(nodata), len(nodata))
        for rspAlg in [resamp_alg_deshift, resamp_alg_calc]:
            assert rspAlg in _dict_rspAlg_rsp_Int.keys(), "'%s' is not a supported resampling algorithm." % rspAlg
        if resamp_alg_calc in ['average', 5] and (v or not q):
            warnings.warn("The resampling algorithm 'average' causes sinus-shaped patterns in fft images that will "
                          "affect the precision of the calculated spatial shifts! It is highly recommended to "
                          "choose another resampling algorithm.")

        self.path_out = path_out  # updated by self.set_outpathes
        self.fmt_out = fmt_out
        self.out_creaOpt = out_crea_options
        self.win_pos_XY = wp  # updated by self.get_opt_winpos_winsize()
        self.win_size_XY = ws  # updated by self.get_opt_winpos_winsize()
        self.max_iter = max_iter
        self.max_shift = max_shift
        self.align_grids = align_grids
        self.match_gsd = match_gsd
        self.out_gsd = out_gsd
        self.target_xyGrid = target_xyGrid
        self.rspAlg_DS = resamp_alg_deshift \
            if isinstance(resamp_alg_deshift, str) else _dict_rspAlg_rsp_Int[resamp_alg_deshift]
        self.rspAlg_calc = resamp_alg_calc \
            if isinstance(resamp_alg_calc, str) else _dict_rspAlg_rsp_Int[resamp_alg_calc]
        self.calc_corners = calc_corners
        self.CPUs = CPUs
        self.bin_ws = binary_ws
        self.force_quadratic_win = force_quadratic_win
        self.v = v
        self.path_verbose_out = path_verbose_out
        self.q = q if not v else False  # overridden by v
        self.progress = progress if not q else False  # overridden by q

        self.ignErr = ignore_errors
        self.max_win_sz_changes = 3  # TODO: änderung der window size, falls nach max_iter kein valider match gefunden
        self.ref = None  # type: GeoArray_CoReg # set by self.get_image_params
        self.shift = None  # type: GeoArray_CoReg # set by self.get_image_params
        self.matchBox = None  # type: boxObj # set by self.get_clip_window_properties()
        self.otherBox = None  # type: boxObj # set by self.get_clip_window_properties()
        self.matchWin = None  # type: GeoArray # set by self._get_image_windows_to_match()
        self.otherWin = None  # type: GeoArray # set by self._get_image_windows_to_match()
        self.overlap_poly = None  # set by self._get_overlap_properties()
        self.overlap_percentage = None  # set by self._get_overlap_properties()
        self.overlap_area = None  # set by self._get_overlap_properties()
        self.imfft_gsd = None  # set by self.get_clip_window_properties()
        self.fftw_works = None  # set by self._calc_shifted_cross_power_spectrum()
        self.fftw_win_size_YX = None  # set by calc_shifted_cross_power_spectrum()

        self.x_shift_px = None  # always in shift image units (image coords) # set by calculate_spatial_shifts()
        self.y_shift_px = None  # always in shift image units (image coords) # set by calculate_spatial_shifts()
        self.x_shift_map = None  # set by self.get_updated_map_info()
        self.y_shift_map = None  # set by self.get_updated_map_info()
        self.vec_length_map = None
        self.vec_angle_deg = None
        self.updated_map_info = None  # set by self.get_updated_map_info()
        self.ssim_orig = None  # set by self._validate_ssim_improvement()
        self.ssim_deshifted = None  # set by self._validate_ssim_improvement()
        self._ssim_improved = None  # private attribute to be filled by self.ssim_improved
        self.shift_reliability = None  # set by self.calculate_spatial_shifts()

        self.tracked_errors = []  # expanded each time an error occurs
        self.success = None  # default
        self.deshift_results = None  # set by self.correct_shifts()

        gdal.AllRegister()
        self._get_image_params()
        self._set_outpathes(im_ref, im_tgt)
        self.grid2use = 'ref' if self.shift.xgsd <= self.ref.xgsd else 'shift'
        if self.v:
            print('resolutions: ', self.ref.xgsd, self.shift.xgsd)

        self._get_overlap_properties()

        if self.v and self.path_verbose_out:
            write_shp(os.path.join(self.path_verbose_out, 'poly_imref.shp'), self.ref.poly, self.ref.prj)
            write_shp(os.path.join(self.path_verbose_out, 'poly_im2shift.shp'), self.shift.poly, self.shift.prj)
            write_shp(os.path.join(self.path_verbose_out, 'overlap_poly.shp'), self.overlap_poly, self.ref.prj)

        # FIXME: transform_mapPt1_to_mapPt2(im2shift_center_map, ds_imref.GetProjection(), ds_im2shift.GetProjection())
        # FIXME später basteln für den fall, dass projektionen nicht gleich sind

        # get_clip_window_properties
        self._get_opt_winpos_winsize()
        if not self.q:
            print('Matching window position (X,Y): %s/%s' % (self.win_pos_XY[0], self.win_pos_XY[1]))
        self._get_clip_window_properties()  # sets self.matchBox, self.otherBox and much more

        if self.v and self.path_verbose_out and self.matchBox.mapPoly and self.success is not False:
            write_shp(os.path.join(self.path_verbose_out, 'poly_matchWin.shp'),
                      self.matchBox.mapPoly, self.matchBox.prj)

        self.success = False if self.success is False or not self.matchBox.boxMapYX else None
        self._coreg_info = None  # private attribute to be filled by self.coreg_info property

    def _handle_error(self, error, warn=False, warnMsg=None):
        """Appends the given error to self.tracked_errors, sets self.success to False and raises the error in case
        self.ignore_errors = True.

        :param error:   instance of an error
        :param warn:    whether to give a warning in case error would be ignored otherwise
        :param warnMsg: a custom message for the warning
        :return:
        """

        warn = warn or warnMsg is not None or self.v

        self.tracked_errors.append(error)
        self.success = False

        if self.ignErr and warn:
            warnMsg = repr(error) if not warnMsg else warnMsg
            print('\nWARNING: ' + warnMsg)

        if not self.ignErr:
            raise error

    def _set_outpathes(self, im_ref, im_tgt):
        assert isinstance(im_ref, (GeoArray, str)) and isinstance(im_tgt, (GeoArray, str)), \
            'COREG._set_outpathes() expects two file pathes (string) or two instances of the ' \
            'GeoArray class. Received %s and %s.' % (type(im_ref), type(im_tgt))

        def get_baseN(path): return os.path.splitext(os.path.basename(path))[0]

        # get input pathes
        path_im_ref = im_ref.filePath if isinstance(im_ref, GeoArray) else im_ref
        path_im_tgt = im_tgt.filePath if isinstance(im_tgt, GeoArray) else im_tgt

        if self.path_out:  # this also applies to self.path_out='auto'

            if self.path_out == 'auto':
                dir_out, fName_out = os.path.dirname(path_im_tgt), ''
            else:
                dir_out, fName_out = os.path.split(self.path_out)

            if dir_out and fName_out:
                # a valid output path is given => do nothing
                pass

            else:
                # automatically create an output directory and filename if not given
                if not dir_out:
                    if not path_im_ref:
                        dir_out = os.path.abspath(os.path.curdir)
                    else:
                        dir_out = os.path.dirname(path_im_ref)

                if not fName_out:
                    ext = 'bsq' if self.fmt_out == 'ENVI' else \
                        gdal.GetDriverByName(self.fmt_out).GetMetadataItem(gdal.DMD_EXTENSION)
                    fName_out = fName_out if fName_out not in ['.', ''] else \
                        '%s__shifted_to__%s' % (get_baseN(path_im_tgt), get_baseN(path_im_ref))
                    fName_out = fName_out + '.%s' % ext if ext else fName_out

                self.path_out = os.path.abspath(os.path.join(dir_out, fName_out))

                assert ' ' not in self.path_out, \
                    "The path of the output image contains whitespaces. This is not supported by GDAL."
        else:
            # this only happens if COREG is not instanced from within Python and self.path_out is explicitly set to None
            # => DESHIFTER will return an array
            pass

        if self.v:
            if self.path_verbose_out:
                dir_out, dirname_out = os.path.split(self.path_verbose_out)

                if not dir_out:
                    if self.path_out:
                        self.path_verbose_out = os.path.dirname(self.path_out)
                    else:
                        self.path_verbose_out = \
                            os.path.abspath(os.path.join(os.path.curdir, 'CoReg_verboseOut__%s__shifted_to__%s'
                                                         % (get_baseN(path_im_tgt), get_baseN(path_im_ref))))
                elif dirname_out and not dir_out:
                    self.path_verbose_out = os.path.abspath(os.path.join(os.path.curdir, dirname_out))

                assert ' ' not in self.path_verbose_out, \
                    "'path_verbose_out' contains whitespaces. This is not supported by GDAL."

        else:
            self.path_verbose_out = None

        if self.path_verbose_out and not os.path.isdir(self.path_verbose_out):
            os.makedirs(self.path_verbose_out)

    def _get_image_params(self):
        self.ref = GeoArray_CoReg(self.params, 'ref')
        self.shift = GeoArray_CoReg(self.params, 'shift')
        assert prj_equal(self.ref.prj, self.shift.prj), \
            'Input projections are not equal. Different projections are currently not supported. Got %s / %s.' \
            % (get_proj4info(proj=self.ref.prj), get_proj4info(proj=self.shift.prj))

    def _get_overlap_properties(self):
        overlap_tmp = get_overlap_polygon(self.ref.poly, self.shift.poly, self.v)
        self.overlap_poly = overlap_tmp['overlap poly']  # has to be in reference projection
        self.overlap_percentage = overlap_tmp['overlap percentage']
        self.overlap_area = overlap_tmp['overlap area']

        assert self.overlap_poly, 'The input images have no spatial overlap.'

        # overlap are must at least cover 16*16 pixels
        px_area = self.ref.xgsd * self.ref.ygsd if self.grid2use == 'ref' else self.shift.xgsd * self.shift.ygsd
        px_covered = self.overlap_area / px_area
        assert px_covered > 16 * 16, \
            'Overlap area covers only %s pixels. At least 16*16 pixels are needed.' % px_covered

    def equalize_pixGrids(self):
        """
        Equalize image grids and projections of reference and target image (align target to reference).
        """
        if not (prj_equal(self.ref.prj, self.shift.prj) and self.ref.xygrid_specs == self.shift.xygrid_specs):
            if not self.q:
                print("Equalizing pixel grids and projections of reference and target image...")

            # noinspection PyProtectedMember
            def apply_subset_bandnames_metadata(geoArr_cr):
                # TODO: replace that function with geoArr.get_subset(zslice=slice(band4match, band4match+1))
                # TODO: as soon as all metadata are passed through get_subset()
                zslice = slice(geoArr_cr.band4match, geoArr_cr.band4match+1)

                if geoArr_cr._bandnames:
                    geoArr_cr.bandnames = list(np.array(list(geoArr_cr._bandnames))[zslice])

                if geoArr_cr._metadata is not None:
                    geoArr_cr.metadata = \
                        geoArr_cr._metadata[list(np.array(range(len(geoArr_cr._metadata.columns)))[zslice])].copy()

                return geoArr_cr

            if self.grid2use == 'ref':
                # resample target image to reference image
                self.shift.arr = self.shift[:, :, self.shift.band4match]  # resample the needed band only
                self.shift = apply_subset_bandnames_metadata(self.shift)

                self.shift.reproject_to_new_grid(prototype=self.ref, CPUs=self.CPUs)
                self.shift.band4match = 0  # after resampling there is only one band in the GeoArray

            else:
                # resample reference image to target image
                # FIXME in case of different projections this will change the projection of the reference image!
                self.ref.arr = self.ref[:, :, self.ref.band4match]  # resample the needed band only
                self.ref = apply_subset_bandnames_metadata(self.ref)
                self.ref.reproject_to_new_grid(prototype=self.shift, CPUs=self.CPUs)
                self.ref.band4match = 0  # after resampling there is only one band in the GeoArray

    def show_image_footprints(self):
        """This method is intended to be called from Jupyter Notebook and shows a web map containing the calculated
        footprints of the input images as well as the corresponding overlap area."""
        # TODO different colors for polygons
        assert self.overlap_poly, 'Please calculate the overlap polygon first.'

        try:
            import folium
            import geojson
        except ImportError:
            folium, geojson = None, None
        if not folium or not geojson:
            raise ImportError("This method requires the libraries 'folium' and 'geojson'. They can be installed with "
                              "the shell command 'pip install folium geojson'.")

        refPoly = reproject_shapelyGeometry(self.ref.poly, self.ref.epsg, 4326)
        shiftPoly = reproject_shapelyGeometry(self.shift.poly, self.shift.epsg, 4326)
        overlapPoly = reproject_shapelyGeometry(self.overlap_poly, self.shift.epsg, 4326)
        matchBoxPoly = reproject_shapelyGeometry(self.matchBox.mapPoly, self.shift.epsg, 4326)

        m = folium.Map(location=tuple(np.array(overlapPoly.centroid.coords.xy).flatten())[::-1])
        for poly in [refPoly, shiftPoly, overlapPoly, matchBoxPoly]:
            gjs = geojson.Feature(geometry=poly, properties={})
            folium.GeoJson(gjs).add_to(m)
        return m

    def show_matchWin(self, figsize=(15, 15), interactive=True, after_correction=None, pmin=2, pmax=98):
        """Show the image content within the matching window.

        :param figsize:             <tuple> figure size
        :param interactive:         <bool> whether to return an interactive figure based on 'holoviews' library
        :param after_correction:    True/False: show the image content AFTER shift correction or before
                                    None: show both states - before and after correction (default)
        :param pmin:                percentage to be used for excluding the darkest pixels from stretching (default: 2)
        :param pmax:                percentage to be used for excluding the brightest pixels from stretching
                                    (default: 98)
        :return:
        """
        if interactive:
            # use Holoviews
            try:
                import holoviews as hv
            except ImportError:
                hv = None
            if not hv:
                raise ImportError(
                    "This method requires the library 'holoviews'. It can be installed for Anaconda with "
                    "the shell command 'conda install -c ioam holoviews bokeh'.")
            warnings.filterwarnings('ignore')
            hv.notebook_extension('matplotlib')
            hv.Store.add_style_opts(hv.Image, ['vmin', 'vmax'])

            # hv.Store.option_setters.options().Image = hv.Options('style', cmap='gnuplot2')
            # hv.Store.add_style_opts(hv.Image, ['cmap'])
            # renderer = hv.Store.renderers['matplotlib'].instance(fig='svg', holomap='gif')
            # RasterPlot = renderer.plotting_class(hv.Image)
            # RasterPlot.cmap = 'gray'
            otherWin_corr = self._get_deshifted_otherWin()
            xmin, xmax, ymin, ymax = self.matchBox.boundsMap

            def get_hv_image(geoArr):
                arr_masked = np.ma.masked_equal(geoArr[:], geoArr.nodata)
                vmin = np.nanpercentile(arr_masked.compressed(), pmin)
                vmax = np.nanpercentile(arr_masked.compressed(), pmax)
                arr2plot = rescale_intensity(arr_masked, in_range=(vmin, vmax), out_range='int8')

                return hv.Image(arr2plot, bounds=(xmin, ymin, xmax, ymax))(
                    style={'cmap': 'gray',
                           'vmin': vmin, 'vmax': vmax,
                           'interpolation': 'none'},
                    plot={'fig_inches': figsize, 'show_grid': True})
                #     plot={'fig_size':100, 'show_grid':True})

            hvIm_matchWin = get_hv_image(self.matchWin)
            hvIm_otherWin_orig = get_hv_image(self.otherWin)
            hvIm_otherWin_corr = get_hv_image(otherWin_corr)

            if after_correction is None:
                # view both states
                print('Matching window before and after correction (above and below): ')

                # get layouts (docs on options: http://build.holoviews.org/Tutorials/Options.html)
                layout_before = (hvIm_matchWin + hvIm_matchWin)(plot=dict(fig_inches=figsize))
                layout_after = (hvIm_otherWin_orig + hvIm_otherWin_corr)(plot=dict(fig_inches=figsize))

                # plot!
                imgs = {1: layout_before, 2: layout_after}
                hmap = hv.HoloMap(imgs, kdims=['image']).collate().cols(1)

            else:
                # view state before or after correction
                imgs = {1: hvIm_matchWin, 2: hvIm_otherWin_corr if after_correction else hvIm_otherWin_orig}
                hmap = hv.HoloMap(imgs, kdims=['image'])

            # Construct a HoloMap by evaluating the function over all the keys
            # hmap = hv.HoloMap(imgs_corr, kdims=['image']) +  hv.HoloMap(imgs_corr, kdims=['image'])

            # Construct a HoloMap by defining the sampling on the Dimension
            # dmap = hv.DynamicMap(image_slice, kdims=[hv.Dimension('z_axis', values=keys)])
            warnings.filterwarnings('default')

            return hmap

        else:
            # TODO add titles
            self.matchWin.show(figsize=figsize)
            if after_correction:
                self._get_deshifted_otherWin().show(figsize=figsize, pmin=pmin, pmax=pmax)
            else:
                self.otherWin.show(figsize=figsize, pmin=pmin, pmax=pmax)

    def show_cross_power_spectrum(self, interactive=False):
        """
        Shows a 3D surface of the cross power spectrum resulting from phase correlating the reference and target
        image within the matching window.

        :param interactive:  whether to return an interactice 3D surface plot based on 'plotly' library
        :return:
        """

        if interactive:
            # create plotly 3D surface

            # import plotly.plotly as py # online mode -> every plot is uploaded into online plotly account
            from plotly.offline import iplot, init_notebook_mode
            import plotly.graph_objs as go

            init_notebook_mode(connected=True)

            z_data = self._calc_shifted_cross_power_spectrum()
            data = [go.Surface(z=z_data)]
            layout = go.Layout(
                title='cross power spectrum',
                autosize=False,
                width=1000,
                height=1000,
                margin={'l': 65, 'r': 50, 'b': 65, 't': 90})
            fig = go.Figure(data=data, layout=layout)

            return iplot(fig, filename='SCPS')

        else:
            # use matplotlib
            scps = self._calc_shifted_cross_power_spectrum()
            PLT.subplot_3dsurface(scps.astype(np.float32))

    def _get_opt_winpos_winsize(self):
        # type: (tuple,tuple) -> None
        """
        Calculates optimal window position and size in reference image units according to DGM, cloud_mask and
        trueCornerLonLat.
        """
        # dummy algorithm: get center position of overlap instead of searching ideal window position in whole overlap
        # TODO automatischer Algorithmus zur Bestimmung der optimalen Window Position

        wp = tuple(self.win_pos_XY)
        assert type(self.win_pos_XY) in [tuple, list, np.ndarray], \
            'The window position must be a tuple of two elements. Got %s with %s elements.' % (type(wp), len(wp))
        wp = tuple(wp)

        if None in wp:
            # use centroid point if possible
            overlap_center_pos_x, overlap_center_pos_y = self.overlap_poly.centroid.coords.xy
            wp = (wp[0] if wp[0] else overlap_center_pos_x[0]), (wp[1] if wp[1] else overlap_center_pos_y[0])

            # validate window position
            if not self.overlap_poly.contains(Point(wp)):
                # in case the centroid point is not within overlap area
                if not self.q:
                    warnings.warn("The centroid point of the two input images could not be used as matching window "
                                  "position since it is outside of the overlap area. Instead the so called "
                                  "'representative point' is used. Alternatively you can provide your own window "
                                  "position as input parameter.")

                # -> use representative point: a point that is garanteed to be within overlap polygon
                overlap_center_pos_x, overlap_center_pos_y = self.overlap_poly.representative_point().coords.xy
                wp = overlap_center_pos_x[0], overlap_center_pos_y[0]

            assert self.overlap_poly.contains(Point(wp))

        else:
            # validate window position
            if not self.overlap_poly.contains(Point(wp)):
                self._handle_error(ValueError('The provided window position %s/%s is outside of the overlap '
                                              'area of the two input images. Check the coordinates.' % wp))

        # check if window position is within bad data area if a respective mask has been provided
        for im in [self.ref, self.shift]:
            if im.mask_baddata is not None:
                imX, imY = mapXY2imXY(wp, im.mask_baddata.gt)

                if im.mask_baddata[int(imY), int(imX)] is True:
                    self._handle_error(
                        RuntimeError('According to the provided bad data mask for the %s the chosen window position '
                                     '%s / %s is within a bad data area. Using this window position for coregistration '
                                     'is not reasonable. Please provide a better window position!'
                                     % (im.imName, wp[0], wp[1])))

        self.win_pos_XY = wp
        self.win_size_XY = (int(self.win_size_XY[0]), int(self.win_size_XY[1])) if self.win_size_XY else (512, 512)

    def _get_clip_window_properties(self):
        """Calculate all properties of the matching window and the other window. These windows are used to read the
        corresponding image positions in the reference and the target image.
        hint: Even if X- and Y-dimension of the target window is equal, the output window can be NOT quadratic!
        """
        # FIXME image sizes like 10000*256 are still possible

        wpX, wpY = self.win_pos_XY
        wsX, wsY = self.win_size_XY
        ref_wsX, ref_wsY = (wsX * self.ref.xgsd, wsY * self.ref.ygsd)  # image units -> map units
        shift_wsX, shift_wsY = (wsX * self.shift.xgsd, wsY * self.shift.ygsd)  # image units -> map units
        ref_box_kwargs = {'wp': (wpX, wpY), 'ws': (ref_wsX, ref_wsY), 'gt': self.ref.gt}
        shift_box_kwargs = {'wp': (wpX, wpY), 'ws': (shift_wsX, shift_wsY), 'gt': self.shift.gt}
        matchBox = boxObj(**ref_box_kwargs) if self.grid2use == 'ref' else boxObj(**shift_box_kwargs)
        otherBox = boxObj(**shift_box_kwargs) if self.grid2use == 'ref' else boxObj(**ref_box_kwargs)
        overlapWin = boxObj(mapPoly=self.overlap_poly, gt=self.ref.gt)

        # clip matching window to overlap area
        matchBox.mapPoly = matchBox.mapPoly.intersection(overlapWin.mapPoly)

        # check if matchBox extent touches no data area of the image -> if yes: shrink it
        overlapPoly_within_matchWin = matchBox.mapPoly.intersection(self.overlap_poly)
        if overlapPoly_within_matchWin.area < matchBox.mapPoly.area:
            wsX_start, wsY_start = 1 if wsX >= wsY else wsX / wsY, 1 if wsY >= wsX else wsY / wsX
            box = boxObj(**dict(wp=(wpX, wpY), ws=(wsX_start, wsY_start), gt=matchBox.gt))
            while True:
                box.buffer_imXY(1, 1)
                if not box.mapPoly.within(overlapPoly_within_matchWin):
                    box.buffer_imXY(-1, -1)
                    matchBox = box
                    break

        # move matching window to imref grid or im2shift grid
        mW_rows, mW_cols = (self.ref.rows, self.ref.cols) if self.grid2use == 'ref' else \
            (self.shift.rows, self.shift.cols)
        matchBox.mapPoly = move_shapelyPoly_to_image_grid(matchBox.mapPoly, matchBox.gt, mW_rows, mW_cols, 'NW')

        # check, ob durch Verschiebung auf Grid die matchBox außerhalb von overlap_poly geschoben wurde
        if not matchBox.mapPoly.within(overlapWin.mapPoly):
            # matchPoly weiter verkleinern # 1 px buffer reicht, weil window nur auf das Grid verschoben wurde
            xLarger, yLarger = matchBox.is_larger_DimXY(overlapWin.boundsIm)
            matchBox.buffer_imXY(-1 if xLarger else 0, -1 if yLarger else 0)

        # matching_win direkt auf grid2use (Rundungsfehler bei Koordinatentrafo beseitigen)
        matchBox.imPoly = round_shapelyPoly_coords(matchBox.imPoly, precision=0, out_dtype=int)

        # Check, ob match Fenster größer als anderes Fenster
        if not (matchBox.mapPoly.within(otherBox.mapPoly) or matchBox.mapPoly == otherBox.mapPoly):
            # dann für anderes Fenster kleinstes Fenster finden, das match-Fenster umgibt
            otherBox.boxImYX = get_smallest_boxImYX_that_contains_boxMapYX(matchBox.boxMapYX, otherBox.gt)

        # evtl. kann es sein, dass bei Shift-Fenster-Vergrößerung das shift-Fenster zu groß für den overlap wird
        t_start = time.time()
        while not otherBox.mapPoly.within(overlapWin.mapPoly):
            # -> match Fenster verkleinern und neues otherBox berechnen
            xLarger, yLarger = otherBox.is_larger_DimXY(overlapWin.boundsIm)
            matchBox.buffer_imXY(-1 if xLarger else 0, -1 if yLarger else 0)
            previous_area = otherBox.mapPoly.area
            otherBox.boxImYX = get_smallest_boxImYX_that_contains_boxMapYX(matchBox.boxMapYX, otherBox.gt)

            if previous_area == otherBox.mapPoly.area or time.time() - t_start > 1.5:
                # happens e.g in case of a triangular footprint
                # NOTE: first condition is not always fulfilled -> therefore added timeout of 1.5 sec
                self._handle_error(
                    RuntimeError('Matching window in target image is larger than overlap area but further shrinking '
                                 'the matching window is not possible. Check if the footprints of the input data have '
                                 'been computed correctly.' +
                                 (' Matching window shrinking timed out.' if time.time() - t_start > 5 else '')))
                break  # break out of while loop in order to avoid that code gets stuck here

        # output validation
        for winBox in [matchBox, otherBox]:
            if winBox.imDimsYX[0] < 16 or winBox.imDimsYX[1] < 16:
                self._handle_error(
                    RuntimeError("One of the input images does not have sufficient gray value information "
                                 "(non-no-data values) for placing a matching window at the position %s. "
                                 "Matching failed." % str((wpX, wpY))))

        if self.success is not False:
            # check result -> ProgrammingError if not fulfilled
            def within_equal(inner, outer): return inner.within(outer) or inner.equals(outer)
            assert within_equal(matchBox.mapPoly, otherBox.mapPoly)
            assert within_equal(otherBox.mapPoly, overlapWin.mapPoly)

            self.imfft_gsd = self.ref.xgsd if self.grid2use == 'ref' else self.shift.xgsd
            self.ref.win, self.shift.win = (matchBox, otherBox) if self.grid2use == 'ref' else (otherBox, matchBox)
            self.matchBox, self.otherBox = matchBox, otherBox
            self.ref.win.size_YX = tuple([int(i) for i in self.ref.win.imDimsYX])
            self.shift.win.size_YX = tuple([int(i) for i in self.shift.win.imDimsYX])
            match_win_size_XY = tuple(reversed([int(i) for i in matchBox.imDimsYX]))

            if not self.q and match_win_size_XY != self.win_size_XY:
                print('Target window size %s not possible due to too small overlap area or window position too close '
                      'to an image edge. New matching window size: %s.' % (self.win_size_XY, match_win_size_XY))

                # write_shp('/misc/hy5/scheffler/Temp/matchMapPoly.shp', matchBox.mapPoly,matchBox.prj)
                # write_shp('/misc/hy5/scheffler/Temp/otherMapPoly.shp', otherBox.mapPoly,otherBox.prj)

    def _get_image_windows_to_match(self):
        """Reads the matching window and the other window using subset read, and resamples the other window to the
        resolution and the pixel grid of the matching window. The result consists of two images with the same
        dimensions and exactly the same corner coordinates."""

        match_fullGeoArr = self.ref if self.grid2use == 'ref' else self.shift
        other_fullGeoArr = self.shift if self.grid2use == 'ref' else self.ref

        # matchWin per subset-read einlesen -> self.matchWin.data
        rS, rE, cS, cE = GEO.get_GeoArrayPosition_from_boxImYX(self.matchBox.boxImYX)
        assert np.array_equal(np.abs(np.array([rS, rE, cS, cE])), np.array([rS, rE, cS, cE])) and \
            rE <= match_fullGeoArr.rows and cE <= match_fullGeoArr.cols, \
            'Requested area is not completely within the input array for %s.' % match_fullGeoArr.imName
        self.matchWin = GeoArray(match_fullGeoArr[rS:rE + 1, cS:cE + 1, match_fullGeoArr.band4match],
                                 geotransform=GEO.get_subset_GeoTransform(match_fullGeoArr.gt, self.matchBox.boxImYX),
                                 projection=copy(match_fullGeoArr.prj),
                                 nodata=copy(match_fullGeoArr.nodata))
        self.matchWin.imID = match_fullGeoArr.imID

        # otherWin per subset-read einlesen
        rS, rE, cS, cE = GEO.get_GeoArrayPosition_from_boxImYX(self.otherBox.boxImYX)
        assert np.array_equal(np.abs(np.array([rS, rE, cS, cE])), np.array([rS, rE, cS, cE])) and \
            rE <= other_fullGeoArr.rows and cE <= other_fullGeoArr.cols, \
            'Requested area is not completely within the input array for %s.' % other_fullGeoArr.imName
        self.otherWin = GeoArray(other_fullGeoArr[rS:rE + 1, cS:cE + 1, other_fullGeoArr.band4match],
                                 geotransform=GEO.get_subset_GeoTransform(other_fullGeoArr.gt, self.otherBox.boxImYX),
                                 projection=copy(other_fullGeoArr.prj),
                                 nodata=copy(other_fullGeoArr.nodata))
        self.otherWin.imID = other_fullGeoArr.imID

        # self.matchWin.deepcopy_array()
        # self.otherWin.deepcopy_array()

        if self.v:
            print('Original matching windows:')
            ref_data, shift_data = (self.matchWin[:], self.otherWin[:]) if self.grid2use == 'ref' else \
                (self.otherWin[:], self.matchWin[:])
            PLT.subplot_imshow([ref_data, shift_data], [self.ref.title, self.shift.title], grid=True)

        # resample otherWin.arr to the resolution of matchWin AND make sure the pixel edges are identical
        # (in order to make each image show the same window with the same coordinates)
        # TODO replace cubic resampling by PSF resampling - average resampling leads to sinus like distortions in the
        # TODO fft image that make a precise coregistration impossible. Thats why there is currently no way around
        # TODO cubic resampling.
        tgt_xmin, tgt_xmax, tgt_ymin, tgt_ymax = self.matchBox.boundsMap

        # equalize pixel grids and projection of matchWin and otherWin (ONLY if grids are really different)
        if not (self.matchWin.xygrid_specs == self.otherWin.xygrid_specs and
                prj_equal(self.matchWin.prj, self.otherWin.prj)):
            self.otherWin.arr, self.otherWin.gt = warp_ndarray(self.otherWin.arr,
                                                               self.otherWin.gt,
                                                               self.otherWin.prj,
                                                               self.matchWin.prj,
                                                               out_gsd=(self.imfft_gsd, self.imfft_gsd),
                                                               out_bounds=([tgt_xmin, tgt_ymin, tgt_xmax, tgt_ymax]),
                                                               rspAlg=_dict_rspAlg_rsp_Int[self.rspAlg_calc],
                                                               in_nodata=self.otherWin.nodata,
                                                               CPUs=self.CPUs,
                                                               progress=False)[:2]

        if self.matchWin.shape != self.otherWin.shape:
            self._handle_error(
                RuntimeError('Catched a possible ProgrammingError at window position %s: Bad output of '
                             'get_image_windows_to_match. Reference image shape is %s whereas shift '
                             'image shape is %s.' % (str(self.matchBox.wp), self.matchWin.shape, self.otherWin.shape)),
                warn=True)

        # check of odd dimensions of output images
        rows, cols = [i if i % 2 == 0 else i - 1 for i in self.matchWin.shape]
        self.matchWin.arr, self.otherWin.arr = self.matchWin.arr[:rows, :cols], self.otherWin.arr[:rows, :cols]
        if self.matchWin.box.imDimsYX != self.matchBox.imDimsYX:
            self.matchBox = self.matchWin.box  # update matchBox
            self.otherBox = self.otherWin.box  # update otherBox

        assert self.matchWin.arr is not None and self.otherWin.arr is not None, 'Creation of matching windows failed.'

    @staticmethod
    def _shrink_winsize_to_binarySize(win_shape_YX, target_size=None):
        # type: (tuple, tuple, int , int) -> any
        """Shrinks a given window size to the closest binary window size (a power of 2) -
        separately for X- and Y-dimension.

        :param win_shape_YX:    <tuple> source window shape as pixel units (rows,colums)
        :param target_size:     <tuple> source window shape as pixel units (rows,colums)
        """

        binarySizes = [2 ** i for i in range(3, 14)]  # [8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
        possibSizes_X = [i for i in binarySizes if i <= win_shape_YX[1]]
        possibSizes_Y = [i for i in binarySizes if i <= win_shape_YX[0]]
        if possibSizes_X and possibSizes_Y:
            tgt_size_X, tgt_size_Y = target_size if target_size else (max(possibSizes_X), max(possibSizes_Y))
            closest_to_target_X = int(min(possibSizes_X, key=lambda x: abs(x - tgt_size_X)))
            closest_to_target_Y = int(min(possibSizes_Y, key=lambda y: abs(y - tgt_size_Y)))
            return closest_to_target_Y, closest_to_target_X
        else:
            return None

    def _calc_shifted_cross_power_spectrum(self, im0=None, im1=None, precision=np.complex64):
        """Calculates shifted cross power spectrum for quantifying x/y-shifts.

        :param im0:         reference image
        :param im1:         subject image to shift
        :param precision:   to be quantified as a datatype
        :return:            2D-numpy-array of the shifted cross power spectrum
        """

        im0 = im0 if im0 is not None else self.matchWin[:] if self.matchWin.imID == 'ref' else self.otherWin[:]
        im1 = im1 if im1 is not None else self.otherWin[:] if self.otherWin.imID == 'shift' else self.matchWin[:]

        assert im0.shape == im1.shape, 'The reference and the target image must have the same dimensions.'
        if im0.shape[0] % 2 != 0:
            warnings.warn('Odd row count in one of the match images!')
        if im1.shape[1] % 2 != 0:
            warnings.warn('Odd column count in one of the match images!')

        wsYX = self._shrink_winsize_to_binarySize(im0.shape) if self.bin_ws else im0.shape
        wsYX = ((min(wsYX),) * 2 if self.force_quadratic_win else wsYX) if wsYX else None

        if wsYX not in [None, (0, 0)]:
            time0 = time.time()
            if self.v:
                print('final window size: %s/%s (X/Y)' % (wsYX[1], wsYX[0]))
                # FIXME size of self.matchWin is not updated
                # FIXME CoRegPoints_grid.WIN_SZ is taken from self.matchBox.imDimsYX but this is not updated

            center_YX = np.array(im0.shape) / 2
            xmin, xmax = int(center_YX[1] - wsYX[1] / 2), int(center_YX[1] + wsYX[1] / 2)
            ymin, ymax = int(center_YX[0] - wsYX[0] / 2), int(center_YX[0] + wsYX[0] / 2)

            in_arr0 = im0[ymin:ymax, xmin:xmax].astype(precision)
            in_arr1 = im1[ymin:ymax, xmin:xmax].astype(precision)

            if self.v:
                PLT.subplot_imshow([np.real(in_arr0).astype(np.float32), np.real(in_arr1).astype(np.float32)],
                                   ['FFTin ' + self.ref.title, 'FFTin ' + self.shift.title], grid=True)

            if pyfftw and self.fftw_works is not False:  # if module is installed and working
                fft_arr0 = pyfftw.FFTW(in_arr0, np.empty_like(in_arr0), axes=(0, 1))()
                fft_arr1 = pyfftw.FFTW(in_arr1, np.empty_like(in_arr1), axes=(0, 1))()

                # catch empty output arrays (for some reason this happens sometimes..) -> use numpy fft
                # => this is caused by the call of pyfftw.FFTW. Exactly in that moment the input array in_arr0 is
                #    overwritten with zeros (maybe this is a bug in pyFFTW?)
                if self.fftw_works in [None, True] and (np.std(fft_arr0) == 0 or np.std(fft_arr1) == 0):
                    self.fftw_works = False
                    # recreate input arrays and use numpy fft as fallback
                    in_arr0 = im0[ymin:ymax, xmin:xmax].astype(precision)
                    in_arr1 = im1[ymin:ymax, xmin:xmax].astype(precision)
                    fft_arr0 = np.fft.fft2(in_arr0)
                    fft_arr1 = np.fft.fft2(in_arr1)
                else:
                    self.fftw_works = True
            else:
                fft_arr0 = np.fft.fft2(in_arr0)
                fft_arr1 = np.fft.fft2(in_arr1)

            # GeoArray(fft_arr0.astype(np.float32)).show(figsize=(15,15))
            # GeoArray(fft_arr1.astype(np.float32)).show(figsize=(15,15))

            if self.v:
                print('forward FFTW: %.2fs' % (time.time() - time0))

            eps = np.abs(fft_arr1).max() * 1e-15
            # cps == cross-power spectrum of im0 and im2

            temp = np.array(fft_arr0 * fft_arr1.conjugate()) / (np.abs(fft_arr0) * np.abs(fft_arr1) + eps)

            time0 = time.time()
            if 'pyfft' in globals():
                ifft_arr = pyfftw.FFTW(temp, np.empty_like(temp), axes=(0, 1), direction='FFTW_BACKWARD')()
            else:
                ifft_arr = np.fft.ifft2(temp)
            if self.v:
                print('backward FFTW: %.2fs' % (time.time() - time0))

            cps = np.abs(ifft_arr)
            # scps = shifted cps  => shift the zero-frequency component to the center of the spectrum
            scps = np.fft.fftshift(cps)
            if self.v:
                PLT.subplot_imshow([np.real(in_arr0).astype(np.uint16), np.real(in_arr1).astype(np.uint16),
                                    np.real(fft_arr0).astype(np.uint8), np.real(fft_arr1).astype(np.uint8), scps],
                                   titles=['matching window im0', 'matching window im1',
                                           "fft result im0", "fft result im1", "cross power spectrum"], grid=True)
                PLT.subplot_3dsurface(np.real(scps).astype(np.float32))
        else:
            scps = None
            self._handle_error(
                RuntimeError('The matching window became too small for calculating a reliable match. Matching failed.'))

        self.fftw_win_size_YX = wsYX
        return scps

    @staticmethod
    def _get_peakpos(scps):
        """Returns the row/column position of the peak within the given cross power spectrum.

        :param scps: <np.ndarray> shifted cross power spectrum
        :return:     <np.ndarray> [row, column]
        """
        max_flat_idx = np.argmax(scps)
        return np.array(np.unravel_index(max_flat_idx, scps.shape))

    @staticmethod
    def _get_shifts_from_peakpos(peakpos, arr_shape):
        y_shift = peakpos[0] - arr_shape[0] // 2
        x_shift = peakpos[1] - arr_shape[1] // 2
        return x_shift, y_shift

    @staticmethod
    def _clip_image(im, center_YX, winSzYX):  # TODO this is also implemented in GeoArray

        def get_bounds(YX, wsY, wsX):
            return int(YX[1] - (wsX / 2)), int(YX[1] + (wsX / 2)), int(YX[0] - (wsY / 2)), int(YX[0] + (wsY / 2))

        wsY, wsX = winSzYX
        xmin, xmax, ymin, ymax = get_bounds(center_YX, wsY, wsX)
        return im[ymin:ymax, xmin:xmax]

    def _get_grossly_deshifted_images(self, im0, im1, x_intshift, y_intshift):
        # TODO this is also implemented in GeoArray # this should update ref.win.data and shift.win.data
        # FIXME avoid that matching window gets smaller although shifting it  with the previous win_size would not move
        # FIXME it into nodata-area
        # get_grossly_deshifted_im0
        old_center_YX = np.array(im0.shape) / 2
        new_center_YX = [old_center_YX[0] + y_intshift, old_center_YX[1] + x_intshift]

        x_left = new_center_YX[1]
        x_right = im0.shape[1] - new_center_YX[1]
        y_above = new_center_YX[0]
        y_below = im0.shape[0] - new_center_YX[0]
        maxposs_winsz_x = 2 * min(x_left, x_right)
        maxposs_winsz_y = 2 * min(y_above, y_below)
        if self.force_quadratic_win:
            maxposs_winsz_x = maxposs_winsz_y = min([maxposs_winsz_x, maxposs_winsz_y])

        gdsh_im0 = self._clip_image(im0, new_center_YX, [maxposs_winsz_y, maxposs_winsz_x])

        # get_corresponding_im1_clip
        crsp_im1 = self._clip_image(im1, np.array(im1.shape) / 2, gdsh_im0.shape)

        if self.v:
            PLT.subplot_imshow([self._clip_image(im0, old_center_YX, gdsh_im0.shape), crsp_im1],
                               titles=['reference original', 'target'], grid=True)
            PLT.subplot_imshow([gdsh_im0, crsp_im1], titles=['reference virtually shifted', 'target'], grid=True)
        return gdsh_im0, crsp_im1

    def _find_side_maximum(self, scps):
        # peakR, peakC = self._get_peakpos(scps)
        peakR, peakC = scps.shape[0] // 2, scps.shape[1] // 2
        profileX = scps[peakR, :].flatten()  # row profile with values from left to right
        profileY = scps[:, peakC].flatten()  # column profile with values from top to bottom

        # get scps values of side maxima
        sm_left, sm_right = profileX[peakC - 1], profileX[peakC + 1]
        sm_above, sm_below = profileY[peakR - 1], profileY[peakR + 1]

        sidemax_lr = {'value': max([sm_left, sm_right]),
                      'side': 'left' if sm_left > sm_right else 'right',
                      'direction_factor': -1 if sm_left > sm_right else 1}
        sidemax_ab = {'value': max([sm_above, sm_below]),
                      'side': 'above' if sm_above > sm_below else 'below',
                      'direction_factor': -1 if sm_above > sm_below else 1}

        if self.v:
            print('Horizontal side maximum found %s. value: %s' % (sidemax_lr['side'], sidemax_lr['value']))
            print('Vertical side maximum found %s. value: %s' % (sidemax_ab['side'], sidemax_ab['value']))
            PLT.subplot_2dline([[range(profileX.size), profileX], [range(profileY.size), profileY]],
                               titles=['X-Profile', 'Y-Profile'], shapetuple=(1, 2), grid=True)

        return sidemax_lr, sidemax_ab

    def _calc_integer_shifts(self, scps):
        peakpos = self._get_peakpos(scps)
        x_intshift, y_intshift = self._get_shifts_from_peakpos(peakpos, scps.shape)
        return x_intshift, y_intshift

    def _calc_shift_reliability(self, scps):
        """Calculates a confidence percentage that can be used as an assessment for reliability of the calculated shifts.

        :param scps:    <np.ndarray> shifted cross power spectrum
        :return:
        """

        # calculate mean power at peak
        peakR, peakC = self._get_peakpos(scps)
        power_at_peak = np.mean(scps[peakR - 1:peakR + 2, peakC - 1:peakC + 2])

        # calculate mean power without peak + 3* standard deviation
        scps_masked = scps
        scps_masked[peakR - 1:peakR + 2, peakC - 1:peakC + 2] = -9999
        scps_masked = np.ma.masked_equal(scps_masked, -9999)
        power_without_peak = np.mean(scps_masked) + 2 * np.std(scps_masked)

        # calculate confidence
        confid = 100 - ((power_without_peak / power_at_peak) * 100)
        confid = 100 if confid > 100 else 0 if confid < 0 else confid

        if not self.q:
            print('Estimated reliability of the calculated shifts:  %.1f' % confid, '%')

        return confid

    def _validate_integer_shifts(self, im0, im1, x_intshift, y_intshift):

        if (x_intshift, y_intshift) != (0, 0):
            # temporalily deshift images on the basis of calculated integer shifts
            gdsh_im0, crsp_im1 = self._get_grossly_deshifted_images(im0, im1, x_intshift, y_intshift)

            # check if integer shifts are now gone (0/0)
            scps = self._calc_shifted_cross_power_spectrum(gdsh_im0, crsp_im1)

            if scps is not None:
                if scps.shape[0] < 3 or scps.shape[1] < 3:
                    self._handle_error(RuntimeError('Shifted cross power spectrum became too small for computing the '
                                                    'point of registration. Matching failed.'))
                    return 'invalid', None, None, scps

                peakpos = self._get_peakpos(scps)
                x_shift, y_shift = self._get_shifts_from_peakpos(peakpos, scps.shape)
                if (x_shift, y_shift) == (0, 0):
                    return 'valid', 0, 0, scps
                else:
                    return 'invalid', x_shift, y_shift, scps
            else:
                return 'invalid', None, None, scps
        else:
            return 'valid', 0, 0, None

    def _calc_subpixel_shifts(self, scps):
        sidemax_lr, sidemax_ab = self._find_side_maximum(scps)
        x_subshift = (sidemax_lr['direction_factor'] * sidemax_lr['value']) / (np.max(scps) + sidemax_lr['value'])
        y_subshift = (sidemax_ab['direction_factor'] * sidemax_ab['value']) / (np.max(scps) + sidemax_ab['value'])
        return x_subshift, y_subshift

    @staticmethod
    def _get_total_shifts(x_intshift, y_intshift, x_subshift, y_subshift):
        return x_intshift + x_subshift, y_intshift + y_subshift

    def _get_deshifted_otherWin(self):
        """Returns a de-shifted version of self.otherWin as a GeoArray instance.The output dimensions and geographic
        bounds are equal to those of self.matchWin and geometric shifts are corrected according to the previously
        computed X/Y shifts within the matching window. This allows direct application of algorithms e.g. measuring
        image similarity.

        The image subset that is resampled in this function is always the same that has been resampled during
        computation of geometric shifts (usually the image with the higher geometric resolution).

        :returns:   GeoArray instance of de-shifted self.otherWin
        """

        # shift vectors have been calculated to fit target image onto reference image
        # -> so the shift vectors have to be inverted if shifts are applied to reference image
        coreg_info = self._get_inverted_coreg_info() if self.otherWin.imID == 'ref' else self.coreg_info

        matchFull = self.ref if self.matchWin.imID == 'ref' else self.shift
        otherFull = self.ref if self.otherWin.imID == 'ref' else self.shift

        ds_results = DESHIFTER(otherFull, coreg_info,
                               band2process=otherFull.band4match + 1,
                               clipextent=self.matchBox.mapPoly.bounds,
                               target_xyGrid=matchFull.xygrid_specs,
                               q=True
                               ).correct_shifts()
        return ds_results['GeoArray_shifted']

    def _validate_ssim_improvement(self, v=False):
        """Computes mean structural similarity index between reference and target image before and after correction
        of geometric shifts..

        :param v:   <bool> verbose mode: shows images of the matchWin, otherWin and shifted version of otherWin
        :return:    <tuple> SSIM before an after shift correction
        """

        assert self.success is not None, \
            'Calculate geometric shifts first before trying to measure image similarity improvement!'
        assert self.success in [True, None], \
            'Since calculation of geometric shifts failed, no image similarity improvement can be measured.'

        # get image dynamic range
        dr = max(self.matchWin[:].max(), self.otherWin[:].max()) - \
            min(self.matchWin[:].min(), self.otherWin[:].min())

        # compute SSIM BEFORE shift correction
        # using gaussian weights could lead to value errors in case of small images when the automatically calulated
        # window size exceeds the image size
        self.ssim_orig = calc_ssim(self.matchWin[:], self.otherWin[:], dynamic_range=dr)

        # compute SSIM AFTER shift correction

        # resample otherWin while correcting detected shifts and match geographic bounds of matchWin
        otherWin_deshift_geoArr = self._get_deshifted_otherWin()

        # get the corresponding matchWin data
        matchWinData = self.matchWin[:]

        # check if shapes of two images are equal (due to bug (?), in some cases otherWin_deshift_geoArr does not have
        # the exact same dimensions as self.matchWin -> maybe bounds are handled differently by gdal.Warp)
        if not self.matchWin.shape == otherWin_deshift_geoArr.shape:  # FIXME this seems to be already fixed
            warnings.warn('SSIM input array shapes are not equal! This issue seemed to be already fixed.. ')
            matchFull = self.ref if self.matchWin.imID == 'ref' else self.shift
            matchWinData, _, _ = matchFull.get_mapPos(self.matchBox.mapPoly.bounds, self.matchWin.prj,
                                                      rspAlg='cubic', band2get=matchFull.band4match)
            self.matchWin.clip_to_poly(self.matchBox.mapPoly)

            # at the image edges it is possible that the size of the matchBox must be reduced in order to make array
            # shapes match
            if not matchWinData.shape == otherWin_deshift_geoArr.shape:  # FIXME this seems to be already fixed
                self.matchBox.buffer_imXY(float(-np.ceil(abs(self.x_shift_px))), float(-np.ceil(abs(self.y_shift_px))))
                otherWin_deshift_geoArr = self._get_deshifted_otherWin()
                matchWinData, _, _ = matchFull.get_mapPos(self.matchBox.mapPoly.bounds, self.matchWin.prj,
                                                          rspAlg='cubic', band2get=matchFull.band4match)

            # output validation
            if not matchWinData.shape == otherWin_deshift_geoArr.shape:
                warnings.warn('SSIM input array shapes could not be equalized. SSIM calculation failed. SSIM of the '
                              'de-shifted target image is set to 0.')
                self.ssim_deshifted = 0
                return self.ssim_orig, self.ssim_deshifted

        self.ssim_deshifted = calc_ssim(otherWin_deshift_geoArr[:], matchWinData, dynamic_range=dr)

        if v:
            GeoArray(matchWinData).show()
            self.otherWin.show()
            otherWin_deshift_geoArr.show()

        if not self.q:
            print('Image similarity within the matching window (SSIM before/after correction): %.4f => %.4f'
                  % (self.ssim_orig, self.ssim_deshifted))

        self.ssim_improved = self.ssim_orig <= self.ssim_deshifted

        # write win data to disk
        # outDir = '/home/gfz-fe/scheffler/temp/ssim_debugging/'
        # GeoArray(matchWinData, matchWinGt, matchWinPrj).save(outDir+'matchWinData.bsq')

        # otherWinGt = (self.otherWin.boundsMap[0], self.matchWin.xgsd, 0,
        #               self.otherWin.boundsMap[3], 0, -self.matchWin.imParams.ygsd)
        # GeoArray(self.otherWin.data, therWinGt, self.otherWin.prj).save(outDir+'otherWin.data.bsq')

        # otherWin_deshift_geoArr.save(outDir+''shifted.bsq')

        return self.ssim_orig, self.ssim_deshifted

    @property
    def ssim_improved(self):
        """Returns True if image similarity within the matching window has been improved by correcting the previously
         computed geometric shifts."""
        if self.success is True:
            if self._ssim_improved is None:
                ssim_orig, ssim_deshifted = self._validate_ssim_improvement()
                self._ssim_improved = ssim_orig <= ssim_deshifted
            return self._ssim_improved

    @ssim_improved.setter
    def ssim_improved(self, has_improved):
        self._ssim_improved = has_improved

    def calculate_spatial_shifts(self):
        # type: (COREG) -> str

        """Compute the global X/Y shift between reference and the target image within the matching window.

        :return: 'success' or 'fail'
        """

        if self.success is False:
            return 'fail'

        if self.q:
            warnings.simplefilter('ignore')

        # set self.matchWin and self.otherWin (GeoArray instances)
        self._get_image_windows_to_match()  # 45-90ms

        im0 = self.matchWin[:] if self.matchWin.imID == 'ref' else self.otherWin[:]
        im1 = self.otherWin[:] if self.otherWin.imID == 'shift' else self.matchWin[:]

        if self.v:
            print('Matching windows with equalized spatial resolution:')
            PLT.subplot_imshow([im0, im1], [self.ref.title, self.shift.title], grid=True)

        gsd_factor = self.imfft_gsd / self.shift.xgsd

        if self.v:
            print('gsd_factor', gsd_factor)
        if self.v:
            print('imfft_gsd_mapvalues', self.imfft_gsd)

        # calculate cross power spectrum without any de-shifting applied
        scps = self._calc_shifted_cross_power_spectrum()  # 8-18ms

        if scps is None:
            self.success = False
            warnings.simplefilter('default')

            return 'fail'

        # calculate spatial shifts

        # calculate integer shifts
        count_iter = 1
        x_intshift, y_intshift = self._calc_integer_shifts(scps)

        if (x_intshift, y_intshift) == (0, 0):
            # in case integer shifts are zero
            self.success = True
        else:
            valid_invalid, x_val_shift, y_val_shift, scps = \
                self._validate_integer_shifts(im0, im1, x_intshift, y_intshift)

            while valid_invalid != 'valid':
                count_iter += 1

                if count_iter > self.max_iter:
                    self._handle_error(RuntimeError('No match found in the given window.'))
                    break

                if valid_invalid == 'invalid' and (x_val_shift, y_val_shift) == (None, None):
                    # this happens if matching window became too small
                    self.success = False
                    break

                if not self.q:
                    print('No clear match found yet. Jumping to iteration %s...' % count_iter)
                    print('input shifts: ', x_val_shift, y_val_shift)

                valid_invalid, x_val_shift, y_val_shift, scps = \
                    self._validate_integer_shifts(im0, im1, x_val_shift, y_val_shift)

                # overwrite previous integer shifts if a valid match has been found
                if valid_invalid == 'valid':
                    self.success = True
                    x_intshift, y_intshift = x_val_shift, y_val_shift

        # calculate sub-pixel shifts
        if self.success or self.success is None:
            # get total pixel shifts
            x_subshift, y_subshift = self._calc_subpixel_shifts(scps)
            x_totalshift, y_totalshift = self._get_total_shifts(x_intshift, y_intshift, x_subshift, y_subshift)

            if max([abs(x_totalshift), abs(y_totalshift)]) > self.max_shift:
                self._handle_error(
                    RuntimeError("The calculated shift (X: %s px / Y: %s px) is recognized as too large to "
                                 "be valid. If you know that it is valid, just set the '-max_shift' "
                                 "parameter to an appropriate value. Otherwise try to use a different window "
                                 "size for matching via the '-ws' parameter or define the spectral bands "
                                 "to be used for matching manually ('-br' and '-bs')."
                                 % (x_totalshift, y_totalshift)))
            else:
                self.success = True
                self.x_shift_px, self.y_shift_px = x_totalshift * gsd_factor, y_totalshift * gsd_factor

                # get map shifts
                new_originX, new_originY = imXY2mapXY((self.x_shift_px, self.y_shift_px), self.shift.gt)
                self.x_shift_map, self.y_shift_map = new_originX - self.shift.gt[0], new_originY - self.shift.gt[3]

                # get length of shift vector in map units
                self.vec_length_map = float(np.sqrt(self.x_shift_map ** 2 + self.y_shift_map ** 2))

                # get angle of shift vector
                self.vec_angle_deg = GEO.angle_to_north((self.x_shift_px, self.y_shift_px)).tolist()[0]

                # print results
                if not self.q:
                    print('Detected integer shifts (X/Y):                            %s/%s' % (x_intshift, y_intshift))
                    print('Detected subpixel shifts (X/Y):                           %s/%s' % (x_subshift, y_subshift))
                    print('Calculated total shifts in fft pixel units (X/Y):         %s/%s'
                          % (x_totalshift, y_totalshift))
                    print('Calculated total shifts in reference pixel units (X/Y):   %s/%s'
                          % (x_totalshift, y_totalshift))
                    print('Calculated total shifts in target pixel units (X/Y):      %s/%s'
                          % (self.x_shift_px, self.y_shift_px))
                    print('Calculated map shifts (X,Y):\t\t\t\t  %s/%s' % (self.x_shift_map, self.y_shift_map))
                    print('Calculated absolute shift vector length in map units:     %s' % self.vec_length_map)
                    print('Calculated angle of shift vector in degrees from North:   %s' % self.vec_angle_deg)

        if self.x_shift_px or self.y_shift_px:
            self._get_updated_map_info()

            # set self.ssim_before and ssim_after
            self._validate_ssim_improvement()  # FIXME uses the not updated matchWin size
            self.shift_reliability = self._calc_shift_reliability(scps)

        warnings.simplefilter('default')

        return 'success'

    def _get_updated_map_info(self):
        original_map_info = geotransform2mapinfo(self.shift.gt, self.shift.prj)
        self.updated_map_info = copy(original_map_info)
        self.updated_map_info[3] = str(float(original_map_info[3]) + self.x_shift_map)
        self.updated_map_info[4] = str(float(original_map_info[4]) + self.y_shift_map)
        if not self.q:
            print('Original map info:', original_map_info)
            print('Updated map info: ', self.updated_map_info)

    @property
    def coreg_info(self):
        """A dictionary containing all the information needed to correct the detected global X/Y shift of the target
        image."""

        if self._coreg_info:
            return self._coreg_info
        else:
            if self.success is None:
                self.calculate_spatial_shifts()
            self._coreg_info = {
                'corrected_shifts_px': {'x': self.x_shift_px, 'y': self.y_shift_px},
                'corrected_shifts_map': {'x': self.x_shift_map, 'y': self.y_shift_map},
                'original map info': geotransform2mapinfo(self.shift.gt, self.shift.prj),
                'updated map info': self.updated_map_info,
                'reference projection': self.ref.prj,
                'reference geotransform': self.ref.gt,
                'reference grid': [[self.ref.gt[0], self.ref.gt[0] + self.ref.gt[1]],
                                   [self.ref.gt[3], self.ref.gt[3] + self.ref.gt[5]]],
                'reference extent': {'cols': self.ref.xgsd, 'rows': self.ref.ygsd},  # FIXME not needed anymore
                'success': self.success}
            return self._coreg_info

    def _get_inverted_coreg_info(self):
        """Returns an inverted dictionary of coreg_info that can be passed to DESHIFTER in order to fit the REFERENCE
        image onto the TARGET image."""

        inv_coreg_info = copy(self.coreg_info)
        inv_coreg_info['corrected_shifts_px']['x'] *= -1
        inv_coreg_info['corrected_shifts_px']['y'] *= -1
        inv_coreg_info['corrected_shifts_map']['x'] *= -1
        inv_coreg_info['corrected_shifts_map']['y'] *= -1
        inv_coreg_info['original map info'] = geotransform2mapinfo(self.ref.gt, self.ref.prj)
        inv_coreg_info['reference geotransform'] = self.shift.gt
        inv_coreg_info['reference grid'] = self.shift.xygrid_specs
        inv_coreg_info['reference projection'] = self.shift.prj

        if inv_coreg_info['updated map info']:
            updated_map_info = copy(inv_coreg_info['original map info'])
            updated_map_info[3] = str(float(inv_coreg_info['original map info'][3]) - self.x_shift_map)
            updated_map_info[4] = str(float(inv_coreg_info['original map info'][4]) - self.y_shift_map)
            inv_coreg_info['updated map info'] = updated_map_info

        return inv_coreg_info

    def correct_shifts(self):
        # type: (COREG) -> dict

        """Correct the already calculated X/Y shift of the target image.

        :return: COREG.deshift_results (dictionary)
        """

        DS = DESHIFTER(self.shift, self.coreg_info,
                       path_out=self.path_out,
                       fmt_out=self.fmt_out,
                       out_crea_options=self.out_creaOpt,
                       out_gsd=self.out_gsd,
                       resamp_alg=self.rspAlg_DS,
                       align_grids=self.align_grids,
                       match_gsd=self.match_gsd,
                       target_xyGrid=self.target_xyGrid,
                       nodata=self.shift.nodata,
                       CPUs=self.CPUs,
                       progress=self.progress,
                       v=self.v,
                       q=self.q)
        self.deshift_results = DS.correct_shifts()
        return self.deshift_results
