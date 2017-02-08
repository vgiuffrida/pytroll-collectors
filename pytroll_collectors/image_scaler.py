# -*- coding: utf-8 -*-

# Copyright (c) 2017

# Author(s):

#   Panu Lahtinen <panu.lahtinen@fmi.fi>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import Queue
import os
import os.path
from ConfigParser import NoOptionError
import logging
import logging.config
import datetime as dt
import glob
from urlparse import urlparse

import numpy as np
from PIL import Image

from posttroll.listener import ListenerContainer
from pycoast import ContourWriter
from trollsift import parse, compose
from mpop.projector import get_area_def

try:
    from mpop.imageo.geo_image import GeoImage
except ImportError:
    GeoImage = None

GSHHS_DATA_ROOT = os.environ['GSHHS_DATA_ROOT']


class ImageScaler(object):

    '''Class for scaling images to defined sizes.'''

    # Config options for the current received message
    out_dir = ''
    update_existing = False
    is_backup = False
    subject = None
    crops = []
    sizes = []
    tags = []
    timeliness = 10
    latest_composite_image = None
    areaname = None
    in_pattern = None
    fileparts = {}
    out_pattern = None
    text_pattern = None
    text_settings = None
    area_def = None
    overlay_config = None
    filepath = None
    existing_fname_parts = None

    def __init__(self, config):
        self.config = config
        topics = config.sections()
        self.listener = ListenerContainer(topics=topics)
        self._loop = True
        self._overlays = {}
        self._cw = ContourWriter(GSHHS_DATA_ROOT)

    def stop(self):
        '''Stop scaler before shutting down.'''
        if self._loop:
            self._loop = False
            if self.listener is not None:
                self.listener.stop()

    def _update_current_config(self):
        """Update the current config to class attributes."""
        try:
            self.out_dir = self.config.get(self.subject, 'out_dir')
        except NoOptionError:
            logging.debug("No config for %s", self.subject)
            pass  # continue

        try:
            self.update_existing = self.config.getboolean(self.subject,
                                                          'update_existing')
        except NoOptionError:
            logging.debug("No option 'update_existing' given, "
                          "default to False")
            self.update_existing = False

        try:
            self.is_backup = self.config.getboolean(self.subject,
                                                    'only_backup')
        except NoOptionError:
            logging.debug("No option 'only_backup' given, "
                          "default to False")
            self.is_backup = False

            # Collect crop information
            self.crops = []
            try:
                crop_conf = self.config.get(self.subject, 'crops').split(',')
            except NoOptionError:
                pass

            for crop in crop_conf:
                if 'x' in crop and '+' in crop:
                    # Crop strings are formated like this:
                    # <x_size>x<y_size>+<x_start>+<y_start>
                    # eg. 1000x300+103+200
                    # Origin (0, 0) is at top-left
                    parts = crop.split('+')
                    crop = tuple(map(int, parts[1:]) +
                                 map(int, parts[0].split('x')))

                    self.crops.append(crop)
                else:
                    self.crops.append(None)

            # Read the requested sizes from configuration section
            # named like the message topic
            self.sizes = []
            for size in self.config.get(self.subject, 'sizes').split(','):
                self.sizes.append(map(int, size.split('x')))

            self.tags = [tag for tag in self.config.get(self.subject,
                                                        'tags').split(',')]
            # get timeliness from config, if available
            try:
                self.timeliness = self.config.getint(self.subject,
                                                     'timeliness')
            except NoOptionError:
                logging.debug("No timeliness given, using default of 10 min")
                self.timeliness = 10

            try:
                self.latest_composite_image = \
                    self.config.get(self.subject, "latest_composite_image")
            except NoOptionError:
                self.latest_composite_image = None

            # get areaname from config
            self.areaname = self.config.get(self.subject, 'areaname')

            # get the input file pattern and replace areaname
            in_pattern = self.config.get(self.subject, 'in_pattern')
            self.in_pattern = in_pattern.replace('{areaname}', self.areaname)

            # parse filename parts from the incoming file
            try:
                fileparts = parse(self.in_pattern,
                                  os.path.basename(self.filepath))
            except ValueError:
                logging.info("Filepattern doesn't match, skipping.")
                pass  # continue
            self.fileparts['areaname'] = self.areaname

            try:
                use_platform_name_hack = \
                    self.config.getboolean(self.subject,
                                           'use_platform_name_hack')
            except NoOptionError:
                # return
                use_platform_name_hack = False

            if use_platform_name_hack:
                # remove "-" from platform names
                self.fileparts['platform_name'] = \
                    self.fileparts['platform_name'].replace('-', '')

            # Check if there's a composite_stack to be updated

            # form the output filename
            out_pattern = self.config.get(self.subject, 'out_pattern')
            self.out_pattern = os.path.join(self.out_dir, out_pattern)

            # Read overlay text settings
            try:
                self.text_pattern = self.config.get(self.subject, 'text')
                self.text_settings = _get_text_settings(self.config,
                                                        self.subject)
            except NoOptionError:
                self.text = None

            # area definition for geoimages and overlays
            try:
                self.area_def = get_area_def(self.config.get(self.subject,
                                                             'areaname'))
            except NoOptionError:
                self.area_def = None
            try:
                self.overlay_config = self.config.get(self.subject,
                                                      'overlay_config')
            except NoOptionError:
                self.overlay_config = None
            # area_def = (area_def.proj4_string, area_def.area_extent)

    def add_overlays(self, img):
        """Add overlays to image.  Add to cache, if not already there."""
        if self.overlay_config is None:
            return img

        if self.subject not in self._overlays:
            logging.debug("Adding overlay to cache")
            self._overlays[self.subject] = \
                self._cw.add_overlay_from_config(self.overlay_config,
                                                 self.area_def)
        else:
            logging.debug("Using overlay from cache")

        return add_overlays(img,  self._overlays[self.subject])

    def _check_existing(self, start_time):
        """Check if there's an existing product that should be updated"""

        # check if something silmiar has already been made:
        # checks for: platform_name, areaname and
        # start_time +- timeliness minutes
        check_start_time = start_time - \
            dt.timedelta(minutes=self.timeliness)
        check_dict = self.fileparts.copy()
        check_dict["tag"] = self.tags[0]
        if self.is_backup:
            check_dict["platform_name"] = '*'
            check_dict["sat_loc"] = '*'
        check_dict["composite"] = '*'

        first_overpass = True
        update_fname_parts = None
        for i in range(2 * self.timeliness + 1):
            check_dict['time'] = check_start_time + dt.timedelta(minutes=i)
            glob_pattern = compose(os.path.join(self.out_dir,
                                                self.out_pattern),
                                   check_dict)
            glob_fnames = glob.glob(glob_pattern)
            if len(glob_fnames) > 0:
                first_overpass = False
                logging.debug("Found files: %s", str(glob_fnames))
                try:
                    update_fname_parts = parse(self.out_pattern,
                                               glob_fnames[0])
                    update_fname_parts["composite"] = \
                        self.fileparts["composite"]
                    if not self.is_backup:
                        try:
                            update_fname_parts["platform_name"] = \
                                self.fileparts["platform_name"]
                        except KeyError:
                            pass
                    break
                except ValueError:
                    logging.debug("Parsing failed for update_fname_parts.")
                    logging.debug("out_pattern: %s, basename: %s",
                                  self.out_pattern, glob_fnames[0])
                    update_fname_parts = {}

        if self.is_backup and not first_overpass:
            logging.info("File already exists, no backuping needed.")
            return None

    def run(self):
        '''Start waiting for messages.

        On message arrival, read the image, scale down to the defined
        sizes and add coastlines.
        '''

        while self._loop:
            # Wait for new messages
            try:
                msg = self.listener.queue.get(True, 5)
            except KeyboardInterrupt:
                self.stop()
                raise
            except Queue.Empty:
                continue

            logging.info("New message with topic %s", msg.subject)

            self.subject = msg.subject
            self.filepath = urlparse(msg.data["uri"]).path

            self._update_current_config()

            self.existing_fname_parts = \
                self._check_existing(msg.data["start_time"])

            # There is already a matching image which isn't going to
            # be updated
            if self.existing_fname_parts is None:
                continue

            # Read the image
            img = read_image(self.filepath)

            # Add overlays, if any
            img = self.add_overlays(img)

            # Save image(s)
            self.save_images(img)

    def save_images(self, img):
        """Save image(s)"""
        # Loop through different image sizes
        for i in range(len(self.sizes)):

            # Crop the image
            img = crop_image(img, self.crops[i])

            # Resize the image
            img = resize_image(img, self.sizes[i])

            # Update existing image if configured to do so
            if self.update_existing:
                img, fname = self._update_existing_img(img, self.tags[i])
                # Add text
                img_out = self._add_text(img, update_img=True)
            else:
                # Add text
                img_out = self._add_text(img, update_img=False)
                # Compose filename
                self.fileparts['tag'] = self.tags[i]
                fname = compose(self.out_pattern, self.fileparts)

            # Save image
            img_out.save(fname)

            # Update latest composite image, if given in config
            if self.latest_composite_image:
                fname = \
                    compose(os.path.join(self.out_dir,
                                         self.latest_composite_image),
                            self.fileparts)
                img = self._update_existing_img(img, self.tags[i],
                                                fname=fname)
                img = self._add_text(img, update_img=False)

                img.save(fname)
                logging.info("Updated latest composite image %s",
                             fname)

    def _add_text(self, img, update_img=False):
        """Add text to the given image"""
        if self.text_pattern is None:
            return img

        if update_img:
            text = compose(self.text_pattern, self.existing_fname_parts)
        else:
            text = compose(self.text_pattern, self.fileparts)

        return add_text(img, text, self.text_settings)

    def _update_existing_img(self, img, tag, fname=None):
        """Update existing image"""
        if fname is None:
            self.existing_fname_parts['tag'] = tag
            fname = compose(os.path.join(self.out_dir, self.out_pattern),
                            self.existing_fname_parts)
        logging.info("Updating image %s with image %s",
                     fname, self.filepath)
        img_out = update_existing_image(fname, img)

        return img_out


def resize_image(img, size):
    """Resize given image to size (x_size, y_size)"""
    x_res, y_res = size

    if img.size[0] == x_res and img.size[1] == y_res:
        img_out = img
    else:
        img_out = img.resize((x_res, y_res))

    return img_out


def crop_image(img, crop):
    """Crop the given image"""
    # Adjust limits so that they don't exceed image dimensions
    crop = list(crop)
    if crop[0] < 0:
        crop[0] = 0
    if crop[1] < 0:
        crop[1] = 0
    if crop[2] > img.size[0]:
        crop[2] = img.size[0]
    if crop[3] > img.size[1]:
        crop[3] = img.size[1]

    try:
        if crop is not None:
            img_wrk = img.crop(crop)
        else:
            img_wrk = img
    except IndexError:
        img_wrk = img

    return img_wrk


def save_image(img, fname, adef=None, time_slot=None, fill_value=None):
    """Save image.  In case of area definition and start time are given,
    and the image type is tif, convert first to Geoimage to save geotiff
    """
    if (adef is not None and time_slot is not None and
            fname.lower().endswith(('.tif', '.tiff'))):
        img = _pil_to_geoimage(img, adef=adef, time_slot=time_slot,
                               fill_value=fill_value)
    img.save(fname)


def _pil_to_geoimage(img, adef, time_slot, fill_value=None):
    """Convert PIL image to GeoImage"""
    # Get image mode, widht and height
    mode = img.mode
    width = img.width
    height = img.height

    # TODO: handle other than 8-bit images
    max_val = 255.
    # Convert to Numpy array
    img = np.array(img.getdata()).astype(np.float32)
    img = img.reshape((height, width, len(mode)))

    chans = []
    # TODO: handle P image mode
    if mode == 'L':
        chans.append(np.squeeze(img) / max_val)
    else:
        if mode.endswith('A'):
            mask = img[:, :, -1]
        else:
            mask = False
        for i in range(len(mode)):
            chans.append(np.ma.masked_where(mask, img[:, :, i] / max_val))

    return GeoImage(chans, adef, time_slot, fill_value=fill_value,
                    mode=mode, crange=_get_crange(len(mode)))


def _get_crange(num):
    """Get crange for interval (0, 1) for *num* image channels."""
    tupl = (0., 1.)
    return num * (tupl, )


def _get_text_settings(config, subject):
    """Parse text settings from the config."""
    settings = {}
    try:
        settings['loc'] = config.get(subject, 'text_location')
    except NoOptionError:
        settings['loc'] = 'SW'

    try:
        settings['font_fname'] = config.get(subject, 'font')
    except NoOptionError:
        settings['font_fname'] = None

    try:
        settings['font_size'] = config.getint(subject, 'font_size')
    except NoOptionError:
        settings['font_size'] = 12

    try:
        settings['text_color'] = [int(x) for x in
                                  config.get(subject,
                                             'text_color').split(',')]
    except NoOptionError:
        settings['text_color'] = [0, 0, 0]

    try:
        settings['bg_color'] = [int(x) for x in
                                config.get(subject,
                                           'text_bg_color').split(',')]
    except NoOptionError:
        settings['bg_color'] = [255, 255, 255]

    try:
        settings['x_marginal'] = config.getint(subject, 'x_marginal')
    except NoOptionError:
        settings['x_marginal'] = 10

    try:
        settings['y_marginal'] = config.getint(subject, 'y_marginal')
    except NoOptionError:
        settings['y_marginal'] = 3

    try:
        settings['bg_extra_width'] = config.getint(subject,
                                                   'bg_extra_width')
    except (ValueError, NoOptionError):
        settings['bg_extra_width'] = None

    return settings


def add_text(img, text, settings):
    """Add text to the image"""
    from PIL import ImageDraw, ImageFont

    if 'L' in img.mode:
        mode = 'RGB'
        if 'A' in img.mode:
            mode += 'A'
        logging.info("Converting to %s", mode)
        img = img.convert(mode)

    width, height = img.size
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(settings['font_fname'],
                                  settings['font_size'])
        logging.debug('Font read from %s', settings['font_fname'])
    except (IOError, TypeError):
        try:
            font = ImageFont.load(settings['font_fname'])
            logging.debug('Font read from %s', settings['font_fname'])
        except (IOError, TypeError):
            logging.warning('Falling back to default font')
            font = ImageFont.load_default()

    textsize = draw.textsize(text, font)

    x_marginal = settings['x_marginal']
    y_marginal = settings['y_marginal']
    bg_extra_width = settings['bg_extra_width']

    if 'S' in settings['loc']:
        if 'W' in settings['loc']:
            text_loc = (x_marginal, height - textsize[1] - 2 * y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           height - textsize[1] - 2 * y_marginal,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           height]
            else:
                box_loc = [0, height - textsize[1] - 2 * y_marginal,
                           width, height]
        elif 'E' in settings['loc']:
            text_loc = (width - textsize[0] - x_marginal,
                        height - textsize[1] - 2 * y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           height - textsize[1] - 2 * y_marginal,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           height]
            else:
                box_loc = [0, height - textsize[1] - 2 * y_marginal,
                           width, height]
        # Center
        else:
            text_loc = ((width - textsize[0]) / 2,
                        height - textsize[1] - 2 * y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           height - textsize[1] - 2 * y_marginal,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           height]
            else:
                box_loc = [0, height - textsize[1] - 2 * y_marginal,
                           width, height]
    else:
        if 'W' in settings['loc']:
            text_loc = (x_marginal, y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           0,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           textsize[1] + 2 * y_marginal]
            else:
                box_loc = [0, 0, width, textsize[1] + 2 * y_marginal]
        elif 'E' in settings['loc']:
            text_loc = (width - textsize[0] - x_marginal, 0)  # y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           0,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           textsize[1] + 2 * y_marginal]
            else:
                box_loc = [0, 0, width, textsize[1] + 2 * y_marginal]
        # Center
        else:
            text_loc = ((width - textsize[0]) / 2, 0)  # y_marginal)
            if bg_extra_width is not None:
                box_loc = [text_loc[0] - bg_extra_width,
                           0,
                           text_loc[0] + textsize[0] + bg_extra_width,
                           textsize[1] + 2 * y_marginal]
            else:
                box_loc = [0, 0, width, textsize[1] + 2 * y_marginal]

    draw.rectangle(box_loc, fill=tuple(settings['bg_color']))
    draw.text(text_loc, text, fill=tuple(settings['text_color']),
              font=font)

    return img


def update_existing_image(fname, new_img):
    '''Read image from fname, if present, and update valid data (= not
    black) from img_in.  Return updated image as PIL image.
    '''

    new_img_mode = new_img.mode
    try:
        old_img = Image.open(fname)
    except IOError:
        return new_img

    if new_img_mode == 'LA':
        old_img = np.array(old_img.convert('RGBA'))
        old_img = np.dstack((old_img[:, :, 0], old_img[:, :, -1]))
        new_img = np.array(new_img.convert('RGBA'))
        new_img = np.dstack((new_img[:, :, 0], new_img[:, :, -1]))
    else:
        old_img = np.array(old_img.convert(new_img_mode))
        new_img = np.array(new_img)

    ndims = old_img.shape
    logging.debug("Image dimensions: old_img: %s, new_img: %s", str(ndims),
                  str(new_img.shape))
    if len(ndims) > 1:
        mask = np.max(new_img, -1) > 0
        for i in range(ndims[-1]):
            old_img[mask, i] = new_img[mask, i]
    else:
        mask = new_img > 0
        old_img[mask] = new_img[mask]

    return Image.fromarray(old_img, mode=new_img_mode)


def read_image(filepath):
    """Read the image from *filepath* and return it as PIL image."""
    return Image.open(filepath)


def add_overlays(img, overlay):
    """"""
    logging.info("Adding overlays")

    img.paste(self._overlays[msg.subject],
              mask=self._overlays[msg.subject])

    return img
