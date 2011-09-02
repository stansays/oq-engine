# -*- coding: utf-8 -*-

# Copyright (c) 2010-2011, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License version 3
# only, as published by the Free Software Foundation.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License version 3 for more details
# (a copy is included in the LICENSE file that accompanied this code).
#
# You should have received a copy of the GNU Lesser General Public License
# version 3 along with OpenQuake.  If not, see
# <http://www.gnu.org/licenses/lgpl-3.0.txt> for a copy of the LGPLv3 License.

"""
This module contains the data required to map configuration values into
oq_params columns.
"""

from collections import namedtuple

from openquake.db.models import OqParams

# pylint: disable=C0103
Param = namedtuple('Param', 'column type default modes')

# TODO unify with utils/oqrunner/config_writer.py
CALCULATION_MODE = {
    'Classical': 'classical',
    'Deterministic': 'deterministic',
    'Event Based': 'event_based',
}

ENUM_MAP = {
    'Average Horizontal': 'average',
    'Average Horizontal (GMRotI50)': 'gmroti50',
    'PGA': 'pga',
    'SA': 'sa',
    'PGV': 'pgv',
    'PGD': 'pgd',
    'None': 'none',
    '1 Sided': 'onesided',
    '2 Sided': 'twosided',
}

CALCULATION_MODES = set(CALCULATION_MODE.values())
PARAMS = {}


def define_param(name, column, modes=None, default=None):
    """
    Adds a new parameter definition to the PARAMS dictionary

    If `column` is `None`, the parameter is only checked but not inserted
    in the `oq_params` table.
    """

    if modes is None:
        modes = CALCULATION_MODES
    elif isinstance(modes, basestring):
        modes = set([modes])
    else:
        modes = set(modes)

    assert modes.issubset(CALCULATION_MODES), \
           'unexpected mode(s) %r' % (modes - CALCULATION_MODES)

    def column_type():
        """Returns the `type` object for the column"""
        # pylint: disable=W0212
        return type(OqParams._meta.get_field_by_name(column)[0])

    if column == None:
        PARAMS[name] = Param(column=column, type=None, default=default,
                             modes=modes)
    else:
        PARAMS[name] = Param(column=column, type=column_type(),
                             default=default, modes=modes)


define_param('SITES', 'sites')
define_param('REGION_GRID_SPACING', 'region_grid_spacing')
define_param('REGION_VERTEX', 'region')
define_param('COMPONENT', 'component')
define_param('INTENSITY_MEASURE_TYPE', 'imt')
define_param('GMPE_TRUNCATION_TYPE', 'truncation_type')
define_param('GMPE_MODEL_NAME', 'gmpe_model_name')
define_param('TRUNCATION_LEVEL', 'truncation_level')
define_param('REFERENCE_VS30_VALUE', 'reference_vs30_value')

define_param('PERIOD', 'period', default=0.0)
define_param('DAMPING', 'damping', default=0.0,
             modes=('event_based', 'deterministic'))

define_param('INTENSITY_MEASURE_LEVELS', 'imls', modes='classical')
define_param('POES_HAZARD_MAPS', 'poes', modes='classical')

define_param('GROUND_MOTION_CORRELATION', 'gm_correlated',
             modes=('deterministic', 'event_based'))

define_param('INVESTIGATION_TIME', 'investigation_time', default=0.0,
             modes=('classical', 'event_based'))
define_param('MINIMUM_MAGNITUDE', 'min_magnitude', default=0.0,
             modes=('classical', 'event_based'))
define_param('NUMBER_OF_LOGIC_TREE_SAMPLES', 'realizations',
             modes=('classical', 'event_based'))

define_param('NUMBER_OF_GROUND_MOTION_FIELDS_CALCULATIONS',
             'gmf_calculation_number', modes='deterministic')
define_param('RUPTURE_SURFACE_DISCRETIZATION',
             'rupture_surface_discretization', modes='deterministic')

define_param('NUMBER_OF_SEISMICITY_HISTORIES', 'histories',
             modes='event_based')

define_param('REFERENCE_DEPTH_TO_2PT5KM_PER_SEC_PARAM',
             'reference_depth_to_2pt5km_per_sec_param',
             modes=('classical', 'event_based'))
define_param('GMF_RANDOM_SEED', 'gmf_random_seed',
             modes=('event_based', 'deterministic'))

# define_param('VULNERABILITY', 'vulnerability')
# define_param('SINGLE_RUPTURE_MODEL', 'single_rupture_model')
# define_param('EXPOSURE', 'exposure')
define_param('SADIGH_SITE_TYPE', 'sadigh_site_type',
             modes=('classical', 'event_based'))
#define_param('OUTPUT_DIR', 'output_dir')

# classical_psha_simple
define_param('SUBDUCTION_RUPTURE_FLOATING_TYPE',
             'subduction_rupture_floating_type',
             modes=('classical', 'event_based'))
define_param('INCLUDE_GRID_SOURCES', 'include_grid_sources',
             modes=('classical', 'event_based'))
#define_param('LOSS_RATIO_MAP', 'loss_ratio_map')
define_param('AGGREGATE_LOSS_CURVE', 'aggregate_loss_curve')
define_param('SUBDUCTION_FAULT_MAGNITUDE_SCALING_SIGMA',
             'subduction_fault_magnitude_scaling_sigma',
             modes=('classical', 'event_based'))
define_param('TREAT_GRID_SOURCE_AS', 'treat_grid_source_as',
             modes=('classical', 'event_based'))
#define_param('LOSS_MAP', 'loss_map')
define_param('LOSS_CURVES_OUTPUT_PREFIX', 'loss_curves_output_prefix')
define_param('INCLUDE_AREA_SOURCES', 'include_area_sources',
             modes=('classical', 'event_based'))
define_param('TREAT_AREA_SOURCE_AS', 'treat_area_source_as',
             modes=('classical', 'event_based'))
define_param('MAXIMUM_DISTANCE', 'maximum_distance', modes='classical')
define_param('QUANTILE_LEVELS', 'quantile_levels', modes='classical')
define_param('INCLUDE_SUBDUCTION_FAULT_SOURCE',
             'include_subduction_fault_source',
             modes=('classical', 'event_based'))
#define_param('GMPE_LOGIC_TREE_FILE', 'gmpe_logic_tree_file')
define_param('GRID_SOURCE_MAGNITUDE_SCALING_RELATIONSHIP',
             'grid_source_magnitude_scaling_relationship')
define_param('STANDARD_DEVIATION_TYPE', 'standard_deviation_type',
             modes=('classical', 'event_based'))
define_param('SUBDUCTION_FAULT_RUPTURE_OFFSET',
             'subduction_fault_rupture_offset',
             modes=('classical', 'event_based'))
define_param('AREA_SOURCE_DISCRETIZATION',
             'area_source_discretization', modes=('classical', 'event_based'))
define_param('FAULT_MAGNITUDE_SCALING_SIGMA',
             'fault_magnitude_scaling_sigma',
             modes=('classical', 'event_based'))
define_param('RISK_CELL_SIZE', 'risk_cell_size')
#define_param('SOURCE_MODEL_LOGIC_TREE_FILE', 'source_model_logic_tree_file')
define_param('WIDTH_OF_MFD_BIN', 'width_of_mfd_bin',
             modes=('classical', 'event_based'))
define_param('AREA_SOURCE_MAGNITUDE_SCALING_RELATIONSHIP',
             'area_source_magnitude_scaling_relationship',
             modes=('classical', 'event_based'))
define_param('SOURCE_MODEL_LT_RANDOM_SEED', 'source_model_lt_random_seed',
             modes=('classical', 'event_based'))
define_param('INCLUDE_FAULT_SOURCE', 'include_fault_source',
             modes=('classical', 'event_based'))
define_param('FAULT_MAGNITUDE_SCALING_RELATIONSHIP',
             'fault_magnitude_scaling_relationship',
             modes=('classical', 'event_based'))
define_param('SUBDUCTION_RUPTURE_ASPECT_RATIO',
             'subduction_rupture_aspect_ratio',
             modes=('classical', 'event_based'))
define_param('FAULT_SURFACE_DISCRETIZATION', 'fault_surface_discretization',
             modes=('classical', 'event_based'))
define_param('GMPE_LT_RANDOM_SEED', 'gmpe_lt_random_seed',
             modes=('classical', 'event_based'))
define_param('SUBDUCTION_FAULT_SURFACE_DISCRETIZATION',
             'subduction_fault_surface_discretization',
             modes=('classical', 'event_based'))
define_param('CONDITIONAL_LOSS_POE', 'conditional_loss_poe')
define_param('RUPTURE_ASPECT_RATIO', 'rupture_aspect_ratio',
             modes=('classical', 'event_based'))
define_param('COMPUTE_MEAN_HAZARD_CURVE', 'compute_mean_hazard_curve',
             modes='classical')
define_param('SUBDUCTION_FAULT_MAGNITUDE_SCALING_RELATIONSHIP',
             'subduction_fault_magnitude_scaling_relationship',
             modes=('classical', 'event_based'))
define_param('FAULT_RUPTURE_OFFSET', 'fault_rupture_offset',
             modes=('classical', 'event_based'))
define_param('RUPTURE_FLOATING_TYPE', 'rupture_floating_type',
             modes=('classical', 'event_based'))
