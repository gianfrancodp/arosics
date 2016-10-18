# -*- coding: utf-8 -*-
__author__='Daniel Scheffler'

import os
import re
import shutil
import subprocess
import time
import warnings

# custom
import gdal
import numpy as np
import pyfftw
from shapely.geometry import Point

# internal modules
from .DeShifter import DESHIFTER
from .          import geometry  as GEO
from .          import io        as IO
from .          import plotting  as PLT

from py_tools_ds.ptds                      import GeoArray
from py_tools_ds.ptds.geo.coord_calc       import corner_coord_to_minmax, get_corner_coordinates
from py_tools_ds.ptds.geo.vector.topology  import get_footprint_polygon, get_overlap_polygon, \
                                                  get_smallest_boxImYX_that_contains_boxMapYX
from py_tools_ds.ptds.geo.projection       import prj_equal, get_proj4info
from py_tools_ds.ptds.geo.vector.geometry  import boxObj, round_shapelyPoly_coords
from py_tools_ds.ptds.geo.coord_grid       import move_shapelyPoly_to_image_grid
from py_tools_ds.ptds.geo.coord_trafo      import pixelToMapYX
from py_tools_ds.ptds.geo.raster.reproject import warp_ndarray
from py_tools_ds.ptds.geo.map_info         import geotransform2mapinfo
from py_tools_ds.ptds.numeric.vector       import find_nearest





class imParamObj(object):
    def __init__(self, CoReg_params, imID):
        assert imID in ['ref', 'shift']

        self.imName = 'reference image' if imID == 'ref' else 'image to be shifted'
        self.v = CoReg_params['v']
        self.q = CoReg_params['q'] if not self.v else False

        # set GeoArray
        get_geoArr = lambda p: GeoArray(p) if not isinstance(p,GeoArray) else p
        self.GeoArray = get_geoArr(CoReg_params['im_ref']) if imID == 'ref' else get_geoArr(CoReg_params['im_tgt'])

        # set title to be used in plots
        self.title = os.path.basename(self.GeoArray.filePath) if self.GeoArray.filePath else self.imName

        # set params
        self.prj   = self.GeoArray.projection
        self.gt    = self.GeoArray.geotransform
        self.xgsd  = self.GeoArray.xgsd
        self.ygsd  = self.GeoArray.ygsd
        self.rows  = self.GeoArray.rows
        self.cols  = self.GeoArray.cols
        self.bands = self.GeoArray.bands

        # validate params
        assert self.prj, 'The %s has no projection.' % self.imName
        assert not re.search('LOCAL_CS', self.prj), 'The %s is not georeferenced.' % self.imName
        assert self.gt, 'The %s has no map information.' % self.imName

        # set band4match
        self.band4match = CoReg_params['r_b4match'] if imID == 'ref' else CoReg_params['s_b4match']
        assert self.bands >= self.band4match >= 1, "The %s has %s %s. So its band number to match must be %s%s." \
            % (self.imName, self.bands, 'bands' if self.bands > 1 else 'band', 'between 1 and '
                if self.bands > 1 else '', self.bands)

        # set nodata
        if CoReg_params['nodata'][0 if imID == 'ref' else 1] is not None:
            self.nodata = CoReg_params['nodata'][0 if imID == 'ref' else 1]
        else:
            self.nodata = GEO.find_noDataVal(self.GeoArray)

        # set corner coords
        given_corner_coord = CoReg_params['data_corners_%s' % ('im0' if imID == 'ref' else 'im1')]
        if given_corner_coord is None:
            if CoReg_params['calc_corners']:
                if not CoReg_params['q']:
                    print('Calculating actual data corner coordinates for %s...' % self.imName)
                self.corner_coord = GEO.get_true_corner_mapXY(self.GeoArray, self.band4match, self.nodata,
                                        CoReg_params['multiproc'], v=self.v, q=self.q)
            else:
                self.corner_coord = get_corner_coordinates(gt=self.GeoArray.geotransform,
                                                               cols=self.cols,rows=self.rows)
        else:
            self.corner_coord = given_corner_coord

        # set footprint polygon
        self.poly = get_footprint_polygon(self.corner_coord, fix_invalid=True)
        for XY in self.corner_coord:
            assert self.poly.contains(Point(XY)) or self.poly.touches(Point(XY)), \
                "The corner position '%s' is outside of the %s." % (XY, self.imName)

        if not CoReg_params['q']: print('Corner coordinates of %s:\n\t%s' % (self.imName, self.corner_coord))


class COREG(object):
    def __init__(self, im_ref, im_tgt, path_out='.', r_b4match=1, s_b4match=1, wp=(None,None), ws=(512, 512),
                 max_iter=5, max_shift=5, align_grids=False, match_gsd=False, out_gsd=None, data_corners_im0=None,
                 data_corners_im1=None, nodata=(None,None), calc_corners=True, multiproc=True, binary_ws=True,
                 force_quadratic_win=True, v=False, path_verbose_out=None, q=False, ignore_errors=False): # FIXME path_im0/1 can also be a GeoArray -> doc
        """
        :param im_ref(str, GeoArray):   source path of reference image (any GDAL compatible image format is supported)
        :param im_tgt(str, GeoArray):   source path of image to be shifted (any GDAL compatible image format is supported)
        :param path_out(str):           target path of the coregistered image
                                        (default: /dir/of/im1/<im1>__shifted_to__<im0>.bsq)
        :param r_b4match(int):          band of reference image to be used for matching (starts with 1; default: 1)
        :param s_b4match(int):          band of shift image to be used for matching (starts with 1; default: 1)
        :param wp(tuple):               custom matching window position as map values in the same projection like the
                                        reference image (default: central position of image overlap)
        :param ws(tuple):               custom matching window size [pixels] (default: (512,512))
        :param max_iter(int):           maximum number of iterations for matching (default: 5)
        :param max_shift(int):          maximum shift distance in reference image pixel units (default: 5 px)
        :param align_grids(bool):       align the coordinate grids of the image to be and the reference image (default: 0)
        :param match_gsd(bool):         match the output pixel size to pixel size of the reference image (default: 0)
        :param out_gsd(tuple):          xgsd ygsd: set the output pixel size in map units
                                        (default: original pixel size of the image to be shifted)
        :param rspAlg(str)              the resampling algorithm to be used if neccessary
        :param data_corners_im0(list):  map coordinates of data corners within reference image
        :param data_corners_im1(list):  map coordinates of data corners within image to be shifted
        :param nodata(tuple):           no data values for reference image and image to be shifted
        :param calc_corners(bool):      calculate true positions of the dataset corners in order to get a useful
                                        matching window position within the actual image overlap
                                        (default: 1; deactivated if '-cor0' and '-cor1' are given
        :param multiproc(bool):         enable multiprocessing (default: 1)
        :param binary_ws(bool):         use binary X/Y dimensions for the matching window (default: 1)
        :param force_quadratic_win(bool):   force a quadratic matching window (default: 1)
        :param v(bool):                 verbose mode (default: 0)
        :param path_verbose_out(str):   an optional output directory for intermediate results
                                        (if not given, no intermediate results are written to disk)
        :param q(bool):                 quiet mode (default: 0)
        :param ignore_errors(bool):     Useful for batch processing. (default: 0)
                                        In case of error COREG.success == False and COREG.x_shift_px/COREG.y_shift_px
                                        is None
        """

        # FIXME add resamp_alg  # FIXME resamp_alg: "average" causes sinus-shaped patterns in fft images
        self.params              = dict([x for x in locals().items() if x[0] != "self"])

        if match_gsd and out_gsd: warnings.warn("'-out_gsd' is ignored because '-match_gsd' is set.\n")
        if out_gsd:  assert isinstance(out_gsd, list) and len(out_gsd) == 2, 'out_gsd must be a list with two values.'
        if data_corners_im0 and not isinstance(data_corners_im0[0],list): # group if not [[x,y],[x,y]..] but [x,y,x,y,]
            data_corners_im0 = [data_corners_im0[i:i+2] for i in range(0, len(data_corners_im0), 2)]
        if data_corners_im1 and not isinstance(data_corners_im1[0],list): # group if not [[x,y],[x,y]..]
            data_corners_im1 = [data_corners_im1[i:i+2] for i in range(0, len(data_corners_im1), 2)]
        if nodata: assert isinstance(nodata, tuple) and len(nodata) == 2, "'nodata' must be a tuple with two values." \
                                                                          "Got %s with length %s." %(type(nodata),len(nodata))

        self.path_out            = path_out            # updated by self.set_outpathes
        self.win_pos_XY          = wp                  # updated by self.get_opt_winpos_winsize()
        self.win_size_XY         = ws                  # updated by self.get_opt_winpos_winsize()
        self.max_iter            = max_iter
        self.max_shift           = max_shift
        self.align_grids         = align_grids
        self.match_gsd           = match_gsd
        self.out_gsd             = out_gsd
        self.calc_corners        = calc_corners
        self.mp                  = multiproc
        self.bin_ws              = binary_ws
        self.force_quadratic_win = force_quadratic_win
        self.v                   = v
        self.path_verbose_out    = path_verbose_out
        self.q                   = q if not v else False
        self.ignErr              = ignore_errors
        self.max_win_sz_changes  = 3                   # TODO: änderung der window size, falls nach max_iter kein valider match gefunden
        self.ref                 = None                # set by self.get_image_params
        self.shift               = None                # set by self.get_image_params
        self.matchWin            = None                # set by self.get_clip_window_properties()
        self.otherWin            = None                # set by self.get_clip_window_properties()
        self.imfft_gsd           = None                # set by self.get_clip_window_properties()
        self.fftw_win_size_YX    = None                # set by calc_shifted_cross_power_spectrum

        self.x_shift_px          = None                # always in shift image units (image coords) # set by calculate_spatial_shifts()
        self.y_shift_px          = None                # always in shift image units (image coords) # set by calculate_spatial_shifts()
        self.x_shift_map         = None                # set by self.get_updated_map_info()
        self.y_shift_map         = None                # set by self.get_updated_map_info()
        self.updated_map_info    = None                # set by self.get_updated_map_info()

        self.tracked_errors      = []                  # expanded each time an error occurs
        self.success             = False               # default

        gdal.AllRegister()
        self._get_image_params()
        self._set_outpathes(im_ref, im_tgt)
        self.grid2use                 = 'ref' if self.shift.xgsd <= self.ref.xgsd else 'shift'
        if self.v: print('resolutions: ', self.ref.xgsd, self.shift.xgsd)

        overlap_tmp                   = get_overlap_polygon(self.ref.poly, self.shift.poly, self.v)
        self.overlap_poly             = overlap_tmp['overlap poly'] # has to be in reference projection
        assert self.overlap_poly, 'The input images have no spatial overlap.'
        self.overlap_percentage       = overlap_tmp['overlap percentage']
        self.overlap_area             = overlap_tmp['overlap area']

        if self.v and self.path_verbose_out:
            IO.write_shp(os.path.join(self.path_verbose_out, 'poly_imref.shp'),    self.ref.poly,     self.ref.prj)
            IO.write_shp(os.path.join(self.path_verbose_out, 'poly_im2shift.shp'), self.shift.poly,   self.shift.prj)
            IO.write_shp(os.path.join(self.path_verbose_out, 'overlap_poly.shp'),  self.overlap_poly, self.ref.prj)

        ### FIXME: transform_mapPt1_to_mapPt2(im2shift_center_map, ds_imref.GetProjection(), ds_im2shift.GetProjection()) # später basteln für den fall, dass projektionen nicht gleich sind

        # get_clip_window_properties
        self._get_opt_winpos_winsize()
        if not self.q: print('Matching window position (X,Y): %s/%s' % (self.win_pos_XY[0], self.win_pos_XY[1]))
        self._get_clip_window_properties()

        if self.v and self.path_verbose_out and self.matchWin.mapPoly:
            IO.write_shp(os.path.join(self.path_verbose_out, 'poly_matchWin.shp'),
                         self.matchWin.mapPoly, self.matchWin.prj)

        self.success = None if self.matchWin.boxMapYX else False


    def _set_outpathes(self, im_ref, im_tgt):
        get_baseN = lambda path: os.path.splitext(os.path.basename(path))[0]

        # get input pathes
        path_im_ref = im_ref.filePath if isinstance(im_ref, GeoArray) else im_ref
        path_im_tgt = im_tgt.filePath if isinstance(im_tgt, GeoArray) else im_tgt

        if self.path_out:
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
                    fName_out           = fName_out if not fName_out in ['.',''] else '%s__shifted_to__%s.bsq' \
                                            %(get_baseN(path_im_tgt), get_baseN(path_im_ref))

                self.path_out       = os.path.abspath(os.path.join(dir_out,fName_out))

                assert ' ' not in self.path_out, \
                    "The path of the output image contains whitespaces. This is not supported by GDAL."
        else:
            # this only happens if COREG is not instanced from within Python and self.path_out is not set
            # => DESHIFTER will return an array
            pass

        if self.v:
            if self.path_verbose_out:
                dir_out, dirname_out = os.path.split(self.path_verbose_out)

                if not dir_out:
                    if self.path_out:
                        self.path_verbose_out = os.path.dirname(self.path_out)
                    else:
                        self.path_verbose_out = os.path.abspath(os.path.join(os.path.curdir,
                            'CoReg_verboseOut__%s__shifted_to__%s' % (get_baseN(path_im_tgt), get_baseN(path_im_ref))))
                elif dirname_out and not dir_out:
                    self.path_verbose_out = os.path.abspath(os.path.join(os.path.curdir, dirname_out))

                assert ' ' not in self.path_verbose_out, \
                    "'path_verbose_out' contains whitespaces. This is not supported by GDAL."

        else:
            self.path_verbose_out = None

        if self.path_verbose_out and not os.path.isdir(self.path_verbose_out): os.makedirs(self.path_verbose_out)


    def _get_image_params(self):
        self.ref   = imParamObj(self.params,'ref')
        self.shift = imParamObj(self.params,'shift')
        assert prj_equal(self.ref.prj, self.shift.prj), \
            'Input projections are not equal. Different projections are currently not supported. Got %s / %s.'\
            %(get_proj4info(proj=self.ref.prj), get_proj4info(proj=self.shift.prj))


    def _get_opt_winpos_winsize(self):
        # type: (tuple,tuple) -> tuple,tuple
        """Calculates optimal window position and size in reference image units according to DGM, cloud_mask and
        trueCornerLonLat."""
        # dummy algorithm: get center position of overlap instead of searching ideal window position in whole overlap
        # TODO automatischer Algorithmus zur Bestimmung der optimalen Window Position

        wp = tuple(self.win_pos_XY)
        assert type(self.win_pos_XY) in [tuple,list,np.ndarray],\
            'The window position must be a tuple of two elements. Got %s with %s elements.' %(type(wp),len(wp))
        wp = tuple(wp)

        if None in wp:
            overlap_center_pos_x, overlap_center_pos_y = self.overlap_poly.centroid.coords.xy
            wp = (wp[0] if wp[0] else overlap_center_pos_x[0]), (wp[1] if wp[1] else overlap_center_pos_y[0])

        assert self.overlap_poly.contains(Point(wp)), 'The provided window position is outside of the overlap area of '\
                                                      'the two input images. Check the coordinates.'
        self.win_pos_XY  = wp
        self.win_size_XY = (int(self.win_size_XY[0]), int(self.win_size_XY[1])) if self.win_size_XY else (512,512)


    def _get_clip_window_properties(self):
        """Calculate all properties of the matching window and the other window. These windows are used to read the
        corresponding image positions in the reference and the target image.
        hint: Even if X- and Y-dimension of the target window is equal, the output window can be NOT quadratic!
        """
        # FIXME image sizes like 10000*256 are still possible

        wpX,wpY             = self.win_pos_XY
        wsX,wsY             = self.win_size_XY
        ref_wsX, ref_wsY    = (wsX*self.ref.xgsd  , wsY*self.ref.ygsd)   # image units -> map units
        shift_wsX,shift_wsY = (wsX*self.shift.xgsd, wsY*self.shift.ygsd) # image units -> map units
        ref_box_kwargs      = {'wp':(wpX,wpY),'ws':(ref_wsX,ref_wsY)    ,'gt':self.ref.gt  }
        shift_box_kwargs    = {'wp':(wpX,wpY),'ws':(shift_wsX,shift_wsY),'gt':self.shift.gt}
        matchWin            = boxObj(**ref_box_kwargs)   if self.grid2use=='ref' else boxObj(**shift_box_kwargs)
        otherWin            = boxObj(**shift_box_kwargs) if self.grid2use=='ref' else boxObj(**ref_box_kwargs)
        overlapWin          = boxObj(mapPoly=self.overlap_poly,gt=self.ref.gt)

        # clip matching window to overlap area
        matchWin.mapPoly = matchWin.mapPoly.intersection(overlapWin.mapPoly)

        # move matching window to imref grid or im2shift grid
        mW_rows, mW_cols = (self.ref.rows, self.ref.cols) if self.grid2use == 'ref' else (self.shift.rows, self.shift.cols)
        matchWin.mapPoly = move_shapelyPoly_to_image_grid(matchWin.mapPoly, matchWin.gt, mW_rows, mW_cols, 'NW')

        # check, ob durch Verschiebung auf Grid das matchWin außerhalb von overlap_poly geschoben wurde
        if not matchWin.mapPoly.within(overlapWin.mapPoly):
            # matchPoly weiter verkleinern # 1 px buffer reicht, weil window nur auf das Grid verschoben wurde
            xLarger,yLarger = matchWin.is_larger_DimXY(overlapWin.boundsIm)
            matchWin.buffer_imXY(-1 if xLarger else 0, -1 if yLarger else 0)

        # matching_win direkt auf grid2use (Rundungsfehler bei Koordinatentrafo beseitigen)
        matchWin.imPoly = round_shapelyPoly_coords(matchWin.imPoly, precision=0, out_dtype=int)

        # Check, ob match Fenster größer als anderes Fenster
        if not (matchWin.mapPoly.within(otherWin.mapPoly) or matchWin.mapPoly==otherWin.mapPoly):
            # dann für anderes Fenster kleinstes Fenster finden, das match-Fenster umgibt
            otherWin.boxImYX = get_smallest_boxImYX_that_contains_boxMapYX(matchWin.boxMapYX,otherWin.gt)

        # evtl. kann es sein, dass bei Shift-Fenster-Vergrößerung das shift-Fenster zu groß für den overlap wird
        while not otherWin.mapPoly.within(overlapWin.mapPoly):
            # -> match Fenster verkleinern und neues anderes Fenster berechnen
            xLarger, yLarger = otherWin.is_larger_DimXY(overlapWin.boundsIm)
            matchWin.buffer_imXY(-1 if xLarger else 0, -1 if yLarger else 0)
            otherWin.boxImYX = get_smallest_boxImYX_that_contains_boxMapYX(matchWin.boxMapYX,otherWin.gt)

        # check results
        assert matchWin.mapPoly.within(otherWin.mapPoly)
        assert otherWin.mapPoly.within(overlapWin.mapPoly)

        self.imfft_gsd              = self.ref.xgsd       if self.grid2use =='ref' else self.shift.xgsd
        self.ref.win,self.shift.win = (matchWin,otherWin) if self.grid2use =='ref' else (otherWin,matchWin)
        self.matchWin,self.otherWin = matchWin, otherWin
        self.ref.  win.size_YX      = tuple([int(i) for i in self.ref.  win.imDimsYX])
        self.shift.win.size_YX      = tuple([int(i) for i in self.shift.win.imDimsYX])
        match_win_size_XY           = tuple(reversed([int(i) for i in matchWin.imDimsYX]))
        if not self.q and match_win_size_XY != self.win_size_XY:
            print('Target window size %s not possible due to too small overlap area or window position too close '
                  'to an image edge. New matching window size: %s.' %(self.win_size_XY,match_win_size_XY))
        #IO.write_shp('/misc/hy5/scheffler/Temp/matchMapPoly.shp', matchWin.mapPoly,matchWin.prj)
        #IO.write_shp('/misc/hy5/scheffler/Temp/otherMapPoly.shp', otherWin.mapPoly,otherWin.prj)


    def _get_image_windows_to_match(self):
        """Reads the matching window and the other window using subset read, and resamples the other window to the
        resolution and the pixel grid of the matching window. The result consists of two images with the same
        dimensions and exactly the same corner coordinates."""

        is_avail_rsp_average = int(gdal.VersionInfo()[0]) >= 2 #FIXME move to output image generation
        if not is_avail_rsp_average:
            warnings.warn("The GDAL version on this server does not yet support the resampling algorithm "
                          "'average'. This can affect the correct detection of subpixel shifts. To avoid this "
                          "please update GDAL to a version above 2.0.0!")

        self.matchWin.imParams = self.ref   if self.grid2use=='ref' else self.shift
        self.otherWin.imParams = self.shift if self.grid2use=='ref' else self.ref

        # matchWin per subset-read einlesen -> self.matchWin.data
        rS, rE, cS, cE = GEO.get_GeoArrayPosition_from_boxImYX(self.matchWin.boxImYX)
        assert np.array_equal(np.abs(np.array([rS,rE,cS,cE])), np.array([rS,rE,cS,cE])), \
            'Got negative values in gdalReadInputs for %s.' %self.matchWin.imParams.imName
        self.matchWin.data = self.matchWin.imParams.GeoArray[rS:rE,cS:cE, self.matchWin.imParams.band4match-1]

        # otherWin per subset-read einlesen
        rS, rE, cS, cE = GEO.get_GeoArrayPosition_from_boxImYX(self.otherWin.boxImYX)
        assert np.array_equal(np.abs(np.array([rS,rE,cS,cE])), np.array([rS,rE,cS,cE])), \
            'Got negative values in gdalReadInputs for %s.' %self.otherWin.imParams.imName
        self.otherWin.data = self.otherWin.imParams.GeoArray[rS:rE, cS:cE, self.otherWin.imParams.band4match - 1]

        if self.v:
            print('Original matching windows:')
            ref_data, shift_data =  (self.matchWin.data, self.otherWin.data) if self.grid2use=='ref' else \
                                    (self.otherWin.data, self.matchWin.data)
            PLT.subplot_imshow([ref_data, shift_data],[self.ref.title,self.shift.title], grid=True)

        otherWin_subgt = GEO.get_subset_GeoTransform(self.otherWin.gt, self.otherWin.boxImYX)

        # resample otherWin.data to the resolution of matchWin AND make sure the pixel edges are identical
        # (in order to make each image show the same window with the same coordinates)
        # rsp_algor = 5 if is_avail_rsp_average else 2 # average if possible else cubic # OLD
        # TODO replace cubic resampling by PSF resampling - average resampling leads to sinus like distortions in the fft image that make a precise coregistration impossible. Thats why there is currently no way around cubic resampling.
        tgt_xmin,tgt_xmax,tgt_ymin,tgt_ymax = self.matchWin.boundsMap
        self.otherWin.data = warp_ndarray(self.otherWin.data,
                                          otherWin_subgt,
                                          self.otherWin.imParams.prj,
                                          self.matchWin.imParams.prj,
                                          out_gsd    = (self.imfft_gsd, self.imfft_gsd),
                                          out_bounds = ([tgt_xmin, tgt_ymin, tgt_xmax, tgt_ymax]),
                                          rspAlg     = 'cubic',
                                          in_nodata  = self.otherWin.imParams.nodata,
                                          progress   = False) [0]

        if self.matchWin.data.shape != self.otherWin.data.shape:
            self.tracked_errors.append(
                RuntimeError('Bad output of get_image_windows_to_match. Reference image shape is %s whereas shift '
                             'image shape is %s.' % (self.matchWin.data.shape, self.otherWin.data.shape)))
            raise self.tracked_errors[-1]
        rows, cols = [i if i % 2 == 0 else i - 1 for i in self.matchWin.data.shape]
        self.matchWin.data, self.otherWin.data = self.matchWin.data[:rows, :cols], self.otherWin.data[:rows, :cols]

        assert self.matchWin.data is not None and self.otherWin.data is not None, 'Creation of matching windows failed.'


    @staticmethod
    def _shrink_winsize_to_binarySize(win_shape_YX, target_size=None):
        # type: (tuple, tuple, int , int) -> tuple
        """Shrinks a given window size to the closest binary window size (a power of 2) -
        separately for X- and Y-dimension.

        :param win_shape_YX:    <tuple> source window shape as pixel units (rows,colums)
        :param target_size:     <tuple> source window shape as pixel units (rows,colums)
        """

        binarySizes   = [2**i for i in range(3,14)] # [8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]
        possibSizes_X = [i for i in binarySizes if i <= win_shape_YX[1]]
        possibSizes_Y = [i for i in binarySizes if i <= win_shape_YX[0]]
        if possibSizes_X and possibSizes_Y:
            tgt_size_X,tgt_size_Y = target_size if target_size else (max(possibSizes_X),max(possibSizes_Y))
            closest_to_target_X = int(min(possibSizes_X, key=lambda x:abs(x-tgt_size_X)))
            closest_to_target_Y = int(min(possibSizes_Y, key=lambda y:abs(y-tgt_size_Y)))
            return closest_to_target_Y,closest_to_target_X
        else:
            return None


    def _calc_shifted_cross_power_spectrum(self, im0=None, im1=None, precision=np.complex64):
        """Calculates shifted cross power spectrum for quantifying x/y-shifts.

            :param im0:         reference image
            :param im1:         subject image to shift
            :param precision:   to be quantified as a datatype
            :return:            2D-numpy-array of the shifted cross power spectrum
        """

        im0 = im0 if im0 is not None else self.ref.win.data
        im1 = im1 if im1 is not None else self.shift.win.data
        assert im0.shape == im1.shape, 'The reference and the target image must have the same dimensions.'
        if im0.shape[0]%2!=0: warnings.warn('Odd row count in one of the match images!')
        if im1.shape[1]%2!=0: warnings.warn('Odd column count in one of the match images!')

        wsYX = self._shrink_winsize_to_binarySize(im0.shape) if self.bin_ws              else im0.shape
        wsYX = ((min(wsYX),) * 2                            if self.force_quadratic_win else wsYX) if wsYX else None

        if wsYX:
            time0 = time.time()
            if self.v: print('final window size: %s/%s (X/Y)' % (wsYX[1], wsYX[0]))
            center_YX = np.array(im0.shape)/2
            xmin,xmax,ymin,ymax = int(center_YX[1]-wsYX[1]/2), int(center_YX[1]+wsYX[1]/2),\
                                  int(center_YX[0]-wsYX[0]/2), int(center_YX[0]+wsYX[0]/2)
            in_arr0  = im0[ymin:ymax,xmin:xmax].astype(precision)
            in_arr1  = im1[ymin:ymax,xmin:xmax].astype(precision)

            if self.v:
                PLT.subplot_imshow([in_arr0.astype(np.float32), in_arr1.astype(np.float32)],
                               ['FFTin '+self.ref.title,'FFTin '+self.shift.title], grid=True)

            if 'pyfft' in globals():
                fft_arr0 = pyfftw.FFTW(in_arr0,np.empty_like(in_arr0), axes=(0,1))()
                fft_arr1 = pyfftw.FFTW(in_arr1,np.empty_like(in_arr1), axes=(0,1))()
            else:
                fft_arr0 = np.fft.fft2(in_arr0)
                fft_arr1 = np.fft.fft2(in_arr1)
            if self.v: print('forward FFTW: %.2fs' %(time.time() -time0))

            eps = np.abs(fft_arr1).max() * 1e-15
            # cps == cross-power spectrum of im0 and im2

            temp = (fft_arr0 * fft_arr1.conjugate()) / (np.abs(fft_arr0) * np.abs(fft_arr1) + eps)

            time0 = time.time()
            if 'pyfft' in globals():
                ifft_arr = pyfftw.FFTW(temp,np.empty_like(temp), axes=(0,1),direction='FFTW_BACKWARD')()
            else:
                ifft_arr = np.fft.ifft2(temp)
            if self.v: print('backward FFTW: %.2fs' %(time.time() -time0))

            cps = np.abs(ifft_arr)
            # scps = shifted cps
            scps = np.fft.fftshift(cps)
            if self.v:
                PLT.subplot_imshow([in_arr0.astype(np.uint16), in_arr1.astype(np.uint16), fft_arr0.astype(np.uint8),
                                fft_arr1.astype(np.uint8), scps], titles=['matching window im0', 'matching window im1',
                                "fft result im0", "fft result im1", "cross power spectrum"], grid=True)
                PLT.subplot_3dsurface(scps.astype(np.float32))
        else:
            self.tracked_errors.append(
                RuntimeError('The matching window became too small for calculating a reliable match. Matching failed.'))
            if self.ignErr:
                scps = None
            else:
                raise self.tracked_errors[-1]

        self.fftw_win_size_YX = wsYX
        return scps


    @staticmethod
    def _get_peakpos(scps):
        max_flat_idx = np.argmax(scps)
        return np.unravel_index(max_flat_idx, scps.shape)


    @staticmethod
    def _get_shifts_from_peakpos(peakpos, arr_shape):
        y_shift = peakpos[0]-arr_shape[0]//2
        x_shift = peakpos[1]-arr_shape[1]//2
        return x_shift,y_shift


    @staticmethod
    def _clip_image(im, center_YX, winSzYX): # TODO this is also implemented in GeoArray
        get_bounds = lambda YX,wsY,wsX: (int(YX[1]-(wsX/2)),int(YX[1]+(wsX/2)),int(YX[0]-(wsY/2)),int(YX[0]+(wsY/2)))
        wsY,wsX    = winSzYX
        xmin,xmax,ymin,ymax = get_bounds(center_YX,wsY,wsX)
        return im[ymin:ymax,xmin:xmax]


    def _get_grossly_deshifted_images(self, im0, im1, x_intshift, y_intshift): # TODO this is also implemented in GeoArray # this should update ref.win.data and shift.win.data
        # get_grossly_deshifted_im0
        old_center_YX = np.array(im0.shape)/2
        new_center_YX = [old_center_YX[0]+y_intshift, old_center_YX[1]+x_intshift]

        x_left  = new_center_YX[1]
        x_right = im0.shape[1]-new_center_YX[1]
        y_above = new_center_YX[0]
        y_below = im0.shape[0]-new_center_YX[0]
        maxposs_winsz = 2*min(x_left,x_right,y_above,y_below)

        gdsh_im0 = self._clip_image(im0, new_center_YX, [maxposs_winsz, maxposs_winsz])

        # get_corresponding_im1_clip
        crsp_im1  = self._clip_image(im1, np.array(im1.shape) / 2, gdsh_im0.shape)

        if self.v:
            PLT.subplot_imshow([self._clip_image(im0, old_center_YX, gdsh_im0.shape), crsp_im1],
                               titles=['reference original', 'target'], grid=True)
            PLT.subplot_imshow([gdsh_im0, crsp_im1], titles=['reference virtually shifted', 'target'], grid=True)
        return gdsh_im0,crsp_im1


    @staticmethod
    def _find_side_maximum(scps, v=0):
        centerpos = [scps.shape[0]//2, scps.shape[1]//2]
        profile_left  = scps[ centerpos [0]  ,:centerpos[1]+1]
        profile_right = scps[ centerpos [0]  , centerpos[1]:]
        profile_above = scps[:centerpos [0]+1, centerpos[1]]
        profile_below = scps[ centerpos [0]: , centerpos[1]]

        if v:
            max_count_vals = 10
            PLT.subplot_2dline([[range(len(profile_left)) [-max_count_vals:], profile_left[-max_count_vals:]],
                                [range(len(profile_right))[:max_count_vals] , profile_right[:max_count_vals]],
                                [range(len(profile_above))[-max_count_vals:], profile_above[-max_count_vals:]],
                                [range(len(profile_below))[:max_count_vals:], profile_below[:max_count_vals]]],
                                titles =['Profile left', 'Profile right', 'Profile above', 'Profile below'],
                                shapetuple=(2,2))

        get_sidemaxVal_from_profile = lambda pf: np.array(pf)[::-1][1] if pf[0]<pf[-1] else np.array(pf)[1]
        sm_dicts_lr  = [{'side':si, 'value': get_sidemaxVal_from_profile(pf)} \
                        for pf,si in zip([profile_left,profile_right],['left','right'])]
        sm_dicts_ab  = [{'side':si, 'value': get_sidemaxVal_from_profile(pf)} \
                        for pf,si in zip([profile_above,profile_below],['above','below'])]
        sm_maxVal_lr = max([i['value'] for i in sm_dicts_lr])
        sm_maxVal_ab = max([i['value'] for i in sm_dicts_ab])
        sidemax_lr   = [sm for sm in sm_dicts_lr if sm['value'] is sm_maxVal_lr][0]
        sidemax_ab   = [sm for sm in sm_dicts_ab if sm['value'] is sm_maxVal_ab][0]
        sidemax_lr['direction_factor'] = {'left':-1, 'right':1} [sidemax_lr['side']]
        sidemax_ab['direction_factor'] = {'above':-1,'below':1} [sidemax_ab['side']]

        if v:
            print('Horizontal side maximum found %s. value: %s' %(sidemax_lr['side'],sidemax_lr['value']))
            print('Vertical side maximum found %s. value: %s' %(sidemax_ab['side'],sidemax_ab['value']))

        return sidemax_lr, sidemax_ab


    def _calc_integer_shifts(self, scps):
        peakpos = self._get_peakpos(scps)
        x_intshift, y_intshift = self._get_shifts_from_peakpos(peakpos, scps.shape)
        return x_intshift, y_intshift


    def _validate_integer_shifts(self, im0, im1, x_intshift, y_intshift):

        if (x_intshift, y_intshift)!=(0,0):
            # temporalily deshift images on the basis of calculated integer shifts
            gdsh_im0, crsp_im1 = self._get_grossly_deshifted_images(im0, im1, x_intshift, y_intshift)

            # check if integer shifts are now gone (0/0)
            scps = self._calc_shifted_cross_power_spectrum(gdsh_im0, crsp_im1)
            if scps is not None:
                peakpos = self._get_peakpos(scps)
                x_shift, y_shift = self._get_shifts_from_peakpos(peakpos, scps.shape)
                if (x_shift, y_shift) == (0,0):
                    return 'valid', 0, 0, scps
                else:
                    return 'invalid', x_shift, y_shift, scps
            else:
                return 'invalid', None, None, scps
        else:
            return 'valid', 0, 0, None


    def calc_subpixel_shifts(self,scps):
        sidemax_lr, sidemax_ab = self._find_side_maximum(scps, self.v)
        x_subshift = (sidemax_lr['direction_factor']*sidemax_lr['value'])/(np.max(scps)+sidemax_lr['value'])
        y_subshift = (sidemax_ab['direction_factor']*sidemax_ab['value'])/(np.max(scps)+sidemax_ab['value'])
        return x_subshift, y_subshift


    @staticmethod
    def _get_total_shifts(x_intshift, y_intshift, x_subshift, y_subshift):
        return x_intshift+x_subshift, y_intshift+y_subshift


    def calculate_spatial_shifts(self):
        if self.success is False: return None,None

        if self.q:  warnings.simplefilter('ignore')

        # set self.ref.win.data and self.shift.win.data
        self._get_image_windows_to_match()

        im0,im1 = self.ref.win.data, self.shift.win.data

        if self.v:
            print('Matching windows with equalized spatial resolution:')
            PLT.subplot_imshow([im0, im1], [self.ref.title, self.shift.title], grid=True)

        gsd_factor = self.imfft_gsd/self.shift.xgsd

        if self.v: print('gsd_factor',         gsd_factor)
        if self.v: print('imfft_gsd_mapvalues',self.imfft_gsd)

        # calculate cross power spectrum without any de-shifting applied
        scps = self._calc_shifted_cross_power_spectrum()

        ## calculate X/Y shifts for target image ##
        x_shift_px,y_shift_px = None,None # defaults

        if scps is None:
            self.success = False
        else:
            # 1st attempt
            count_iter = 1
            x_intshift, y_intshift = self._calc_integer_shifts(scps)

            if (x_intshift, y_intshift) == (0, 0):
                self.success = True
            else:
                valid_invalid, x_val_shift, y_val_shift, scps = \
                    self._validate_integer_shifts(im0, im1, x_intshift, y_intshift)

                while valid_invalid!='valid':
                    count_iter += 1

                    if count_iter > self.max_iter:
                        self.success = False
                        self.tracked_errors.append(RuntimeError('No match found in the given window.'))
                        if not self.ignErr:
                            raise self.tracked_errors[-1]
                        else:
                            warnings.warn('No match found in the given window.'); break

                    if valid_invalid=='invalid' and (x_val_shift, y_val_shift)==(None, None):
                        # this happens if matching window became too small
                        self.success = False
                        break

                    if not self.q: print('No clear match found yet. Jumping to iteration %s...' % count_iter)
                    if not self.q: print('input shifts: ', x_val_shift, y_val_shift)

                    valid_invalid, x_val_shift, y_val_shift, scps = \
                        self._validate_integer_shifts(im0, im1, x_val_shift, y_val_shift)

                    # overwrite previous integer shifts if a valid match has been found
                    if valid_invalid=='valid':
                        self.success = True
                        x_intshift, y_intshift = x_val_shift, y_val_shift

            if not self.success==False:
                x_subshift,   y_subshift    = self.calc_subpixel_shifts(scps)
                x_totalshift, y_totalshift  = self._get_total_shifts(x_intshift, y_intshift, x_subshift, y_subshift)
                x_shift_px,   y_shift_px    = x_totalshift*gsd_factor, y_totalshift*gsd_factor
                if not self.q:
                    print('Detected integer shifts (X/Y):       %s/%s' %(x_intshift,y_intshift))
                    print('Detected subpixel shifts (X/Y):      %s/%s' %(x_subshift,y_subshift))
                    print('Calculated total shifts in fft pixel units (X/Y):         %s/%s' %(x_totalshift,y_totalshift))
                    print('Calculated total shifts in reference pixel units (X/Y):   %s/%s' %(x_totalshift,y_totalshift))
                    print('Calculated total shifts in target pixel units (X/Y):      %s/%s' %(x_shift_px,y_shift_px))

                if max([abs(x_totalshift),abs(y_totalshift)]) > self.max_shift:
                    self.success = False
                    self.tracked_errors.append(
                        RuntimeError("The calculated shift (X: %s px / Y: %s px) is recognized as too large to "
                                     "be valid. If you know that it is valid, just set the '-max_shift' "
                                     "parameter to an appropriate value. Otherwise try to use a different window "
                                     "size for matching via the '-ws' parameter or define the spectral bands "
                                     "to be used for matching manually ('-br' and '-bs')."
                                     % (x_totalshift, y_totalshift)))
                    if not self.ignErr:
                        raise self.tracked_errors[-1]
                else:
                    self.success = True

        self.x_shift_px, self.y_shift_px = (x_shift_px,y_shift_px) if self.success else (None,None)
        if self.x_shift_px or self.y_shift_px:
            self._get_updated_map_info()

        warnings.simplefilter('default')


    def _get_updated_map_info(self):
        original_map_info = geotransform2mapinfo(self.shift.gt, self.shift.prj)
        new_originY, new_originX = pixelToMapYX([self.x_shift_px,self.y_shift_px],
                                                     geotransform=self.shift.gt, projection=self.shift.prj)[0]
        self.x_shift_map = new_originX - self.shift.gt[0]
        self.y_shift_map = new_originY - self.shift.gt[3]
        if not self.q: print('Calculated map shifts (X,Y):\t\t\t\t ', self.x_shift_map,self.y_shift_map)

        self.updated_map_info    = original_map_info.copy()
        self.updated_map_info[3] = str(float(original_map_info[3]) + self.x_shift_map)
        self.updated_map_info[4] = str(float(original_map_info[4]) + self.y_shift_map)
        if not self.q: print('Original map info:', original_map_info)
        if not self.q: print('Updated map info: ',self.updated_map_info)


    @property
    def coreg_info(self):
        return {
            'corrected_shifts_px'   : {'x':self.x_shift_px,  'y':self.y_shift_px },
            'corrected_shifts_map'  : {'x':self.x_shift_map, 'y':self.y_shift_map},
            'original map info'     : geotransform2mapinfo(self.shift.gt, self.shift.prj),
            'updated map info'      : self.updated_map_info,
            'reference projection'  : self.ref.prj,
            'reference geotransform': self.ref.gt,
            'reference grid'        : [ [self.ref.gt[0], self.ref.gt[0]+self.ref.gt[1]],
                                        [self.ref.gt[3], self.ref.gt[3]+self.ref.gt[5]] ],
            'reference extent'      : {'cols':self.ref.xgsd, 'rows':self.ref.ygsd}, # FIXME not needed anymore
            'success'               : self.success}


    def correct_shifts(self):
        DS = DESHIFTER(self.shift.GeoArray, self.coreg_info, path_out=self.path_out, out_gsd=self.out_gsd,
                       align_grids=self.align_grids, match_gsd=self.match_gsd, v=self.v, q=self.q)
        deshift_results = DS.correct_shifts()
        return deshift_results


    def correct_shifts_OLD(self):
        if self.success:
            if not os.path.exists(os.path.dirname(self.path_out)): os.makedirs(os.path.dirname(self.path_out))
            equal_prj = prj_equal(self.ref.prj, self.shift.prj)

            if equal_prj and not self.align_grids and not self.match_gsd and \
                self.out_gsd in [None,[self.shift.xgsd,self.shift.ygsd]]:
                self._shift_image_by_updating_map_info()
            elif equal_prj and self.align_grids: # match_gsd and out_gsd are respected
                self._align_coordinate_grids()
            else: # match_gsd and out_gsd are respected ### TODO: out_proj implementieren
                self._resample_without_grid_aligning()
        else:
            warnings.warn('No result written because detection of image displacements failed.')


    def _shift_image_by_updating_map_info(self):
        if not self.q: print('\nWriting output...')
        ds_im2shift = gdal.Open(self.shift.path)
        if not ds_im2shift.GetDriver().ShortName == 'ENVI': # FIXME laaangsam
            if self.mp:
                IO.convert_gdal_to_bsq__mp(self.shift.path, self.path_out)
            else:
                os.system('gdal_translate -of ENVI %s %s' %(self.shift.path, self.path_out))
            file2getHdr = self.path_out
        else:
            shutil.copy(self.shift.path,self.path_out)
            file2getHdr = self.shift.path
        ds_im2shift = None

        path_hdr = '%s.hdr' %os.path.splitext(file2getHdr)[0]
        path_hdr = '%s.hdr' %file2getHdr if not os.path.exists(path_hdr) else path_hdr
        path_hdr = None if not os.path.exists(path_hdr) else path_hdr

        assert path_hdr, 'No header file found for %s. Applying shifts failed.' %file2getHdr

        new_originY, new_originX = pixelToMapYX([self.x_shift_px, self.y_shift_px],
                                                    geotransform=self.shift.gt, projection=self.shift.prj)[0]
        map_xshift,  map_yshift  = new_originX - self.shift.gt[0], new_originY - self.shift.gt[3]
        if not self.q: print('Calculated map shifts (X,Y): %s, %s' %(map_xshift,map_yshift))

        with open(path_hdr,'r') as inF:
            content = inF.read()
            map_info_line   = [i for i in content.split('\n') if re.search('map info [\w+]*=',i,re.I)][0]
            map_info_string = re.search('{([\w+\s\S]*)}',map_info_line,re.I).group(1)
            map_info        = [i.strip() for i in map_info_string.split(',')]
        if not self.q: print('Original map info: %s' %map_info)
        new_map_info = map_info
        new_map_info[3] = str(float(map_info[3]) + map_xshift)
        new_map_info[4] = str(float(map_info[4]) + map_yshift)
        if not self.q: print('Updated map info:  %s' %new_map_info)
        new_map_info_line = 'map info = { %s }' %' , '.join(new_map_info)
        new_content = content.replace(map_info_line,new_map_info_line)
        outpath_hdr = '%s.hdr' %os.path.splitext(self.path_out)[0]
        with open(outpath_hdr,'w') as outF:
            outF.write(new_content)

        if not self.q: print('\nCoregistered image written to %s.' %self.path_out)


    def _align_coordinate_grids(self):
        xgsd, ygsd = (self.ref.xgsd, self.ref.ygsd) if self.match_gsd else self.out_gsd if self.out_gsd \
                        else (self.shift.xgsd,self.shift.ygsd) # self.match_gsd overrides self.out_gsd in __init__

        if not self.q:
            print('Resampling shift image in order to align coordinate grid of shift image to reference image. ')

        is_alignable = lambda gsd1,gsd2: max(gsd1,gsd2)%min(gsd1,gsd2)==0 # checks if pixel sizes are divisible
        if not is_alignable(self.ref.xgsd,xgsd) or not is_alignable(self.ref.ygsd,ygsd) and not self.q:
            print("\nWARNING: The coordinate grids of the reference image and the image to be shifted cannot be "
                  "aligned because their pixel sizes are not exact multiples of each other (ref [X/Y]: "
                  "%s %s; shift [X/Y]: %s %s). Therefore the pixel size of the reference image is chosen for the "
                  "resampled output image. If you don´t like that you can use the '-out_gsd' parameter to set an "
                  "appropriate output pixel size.\n" %(self.ref.xgsd,self.ref.ygsd,xgsd,ygsd))
            xgsd, ygsd = self.ref.xgsd, self.ref.ygsd

        r_xmin,r_xmax,r_ymin,r_ymax =  corner_coord_to_minmax(get_corner_coordinates(self.ref.path))
        imref_xgrid = np.arange(r_xmin,r_xmax,self.ref.xgsd)
        imref_ygrid = np.arange(r_ymin,r_ymax,self.ref.ygsd)
        s_xmin,s_xmax,s_ymin,s_ymax =  corner_coord_to_minmax(get_corner_coordinates(self.shift.path))
        xmin,xmax = find_nearest(imref_xgrid, s_xmin, 'off',extrapolate=1), find_nearest(imref_xgrid, s_xmax, 'on',extrapolate=1)
        ymin,ymax = find_nearest(imref_ygrid, s_ymin, 'off',extrapolate=1), find_nearest(imref_ygrid, s_ymax, 'on',extrapolate=1)

        new_originY, new_originX = pixelToMapYX([self.x_shift_px, self.y_shift_px],
                                                 geotransform=self.shift.gt, projection=self.shift.prj)[0]
        map_xshift,  map_yshift  = new_originX - self.shift.gt[0], new_originY - self.shift.gt[3]
        if not self.q: print('Calculated map shifts (X,Y): %s, %s' %(map_xshift,map_yshift))
        gt_shifted                   = list(self.shift.gt[:])
        gt_shifted[0], gt_shifted[3] = new_originX, new_originY

        pathVRT = os.path.splitext(os.path.basename(self.shift.path))[0] + '.vrt'
        ds_im2shift = gdal.Open(self.shift.path)
        dst_ds  = gdal.GetDriverByName('VRT').CreateCopy(pathVRT, ds_im2shift, 0)
        dst_ds.SetGeoTransform(gt_shifted)
        dst_ds = ds_im2shift  = None

        cmd = "gdalwarp -r cubic -tr %s %s -t_srs '%s' -of ENVI %s %s -overwrite -te %s %s %s %s" \
              %(xgsd,ygsd,self.ref.prj,pathVRT,self.path_out, xmin,ymin,xmax,ymax)
        output = subprocess.check_output(cmd, shell=True)
        if output!=1 and os.path.exists(self.path_out):
            if not self.q: print('Coregistered and resampled image written to %s.' %self.path_out)
        else:
            print(output)
            self.tracked_errors.append(RuntimeError('Resampling failed.'))
            raise self.tracked_errors[-1]


    def _resample_without_grid_aligning(self):
        xgsd, ygsd = (self.ref.xgsd, self.ref.ygsd) if self.match_gsd else self.out_gsd if self.out_gsd \
                        else (self.shift.xgsd,self.shift.ygsd) # self.match_gsd overrides self.out_gsd in __init__

        new_originY, new_originX = pixelToMapYX([self.x_shift_px, self.y_shift_px],
                                                 geotransform=self.shift.gt, projection=self.shift.prj)[0]
        map_xshift,  map_yshift  = new_originX - self.shift.gt[0], new_originY - self.shift.gt[3]
        if not self.q: print('Calculated map shifts (X,Y): %s, %s' %(map_xshift,map_yshift))
        gt_shifted                   = list(self.shift.gt[:])
        gt_shifted[0], gt_shifted[3] = new_originX, new_originY

        pathVRT     = os.path.splitext(os.path.basename(self.shift.path))[0] + '.vrt'
        ds_im2shift = gdal.Open(self.shift.path)
        dst_ds  = (lambda drv: drv.CreateCopy(pathVRT, ds_im2shift, 0))(gdal.GetDriverByName('VRT'))
        dst_ds.SetGeoTransform(gt_shifted)
        dst_ds = ds_im2shift = None

        if not self.q: print('Resampling shift image without aligning of coordinate grids. ')
        cmd = "gdalwarp -r cubic -tr %s %s -t_srs '%s' -of ENVI %s %s -overwrite" \
              %(xgsd,ygsd,self.ref.prj,pathVRT,self.path_out)
        output = subprocess.check_output(cmd, shell=True)
        if output!=1 and os.path.exists(self.path_out):
            if not self.q: print('Coregistered and resampled image written to %s.' %self.path_out)
        else:
            print(output)
            self.tracked_errors.append(RuntimeError('Resampling failed.'))
            raise self.tracked_errors[-1]