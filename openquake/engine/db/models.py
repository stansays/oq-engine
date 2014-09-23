# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010-2014, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

# Disable:
# - 'Maximum number of public methods for a class'
# - 'Missing docstring' (because of all of the model Meta)
# pylint: disable=R0904,C0111

'''
Model representations of the OpenQuake DB tables.
'''

import os
import collections
import operator
import itertools
from datetime import datetime


import numpy
from scipy import interpolate

from django.db import connections
from django.core.exceptions import ObjectDoesNotExist

from django.contrib.gis.db import models as djm

from openquake.hazardlib.imt import from_string
from openquake.hazardlib import source, geo, calc, correlation
from openquake.hazardlib.calc import filters
from openquake.hazardlib.site import (
    SiteCollection, FilteredSiteCollection)

from openquake.commonlib.general import distinct
from openquake.commonlib.riskloaders import loss_type_to_cost_type
from openquake.commonlib.readinput import get_mesh
from openquake.commonlib.oqvalidation import OqParam
from openquake.commonlib import logictree

from openquake.engine.db import fields
from openquake.engine import writer

#: Kind of supported curve statistics
STAT_CHOICES = (
    (u'mean', u'Mean'),
    (u'quantile', u'Quantile'))


#: System Reference ID used for geometry objects
DEFAULT_SRID = 4326


VS30_TYPE_CHOICES = (
    (u"measured", u"Value obtained from on-site measurements"),
    (u"inferred", u"Estimated value"),
)

IMT_CHOICES = (
    (u'PGA', u'Peak Ground Acceleration'),
    (u'PGV', u'Peak Ground Velocity'),
    (u'PGD', u'Peak Ground Displacement'),
    (u'SA', u'Spectral Acceleration'),
    (u'IA', u'Arias Intensity'),
    (u'RSD', u'Relative Significant Duration'),
    (u'MMI', u'Modified Mercalli Intensity'),
)

#: Default Loss Curve Resolution used for probabilistic risk calculators
DEFAULT_LOSS_CURVE_RESOLUTION = 50


#: Minimum value for a seed number
MIN_SINT_32 = -(2 ** 31)
#: Maximum value for a seed number
MAX_SINT_32 = (2 ** 31) - 1


#: Kind of supported type of loss outputs
LOSS_TYPES = ["structural", "nonstructural", "fatalities", "contents"]

#: relative tolerance to consider two risk outputs (almost) equal
RISK_RTOL = 0.05


#: absolute tolerance to consider two risk outputs (almost) equal
RISK_ATOL = 0.01

# TODO: these want to be dictionaries
INPUT_TYPE_CHOICES = (
    (u'unknown', u'Unknown'),
    (u'source', u'Source Model'),
    (u'source_model_logic_tree', u'Source Model Logic Tree'),
    (u'gsim_logic_tree', u'Ground Shaking Intensity Model Logic Tree'),
    (u'exposure', u'Exposure'),
    (u'fragility', u'Fragility'),
    (u'site_model', u'Site Model'),
    (u'rupture_model', u'Rupture Model'),

    # vulnerability models
    (u'structural_vulnerability', u'Structural Vulnerability'),
    (u'nonstructural_vulnerability', u'Non Structural Vulnerability'),
    (u'contents_vulnerability', u'Contents Vulnerability'),
    (u'business_interruption_vulnerability',
     u'Business Interruption Vulnerability'),
    (u'occupants_vulnerability', u'Occupants Vulnerability'),
    (u'structural_vulnerability_retrofitted',
     u'Structural Vulnerability Retrofitted'))


VULNERABILITY_TYPE_CHOICES = [choice[0]
                              for choice in INPUT_TYPE_CHOICES
                              if choice[0].endswith('vulnerability')]

RAISE_EXC = object()  # sentinel used in OqJob.get_param


class MissingParameter(KeyError):
    """Raised by OqJob.get_param when a parameter is missing in the database"""


#: The output of HazardCalculation.gen_ruptures
SourceRuptureSites = collections.namedtuple(
    'SourceRuptureSites',
    'source rupture sites')


############## Fix FloatField underflow error ##################
# http://stackoverflow.com/questions/9556586/floating-point-numbers-of-python-float-and-postgresql-double-precision

def _get_prep_value(self, value):
    if value is None:
        return None
    val = float(value)
    if abs(val) < 1E-300:
        return 0.
    return val

djm.FloatField.get_prep_value = _get_prep_value


def risk_almost_equal(o1, o2, key=lambda x: x, rtol=RISK_RTOL, atol=RISK_ATOL):
    return numpy.testing.assert_allclose(
        numpy.array(key(o1)), numpy.array(key(o2)), rtol=rtol, atol=atol)


def loss_curve_almost_equal(curve, expected_curve):
    if getattr(curve, 'asset_value', None) == 0.0 and getattr(
            expected_curve, 'asset_value', None) == 0.0:
        return risk_almost_equal(curve.loss_ratios, expected_curve.loss_ratios)
    elif curve.losses[curve.losses > 0].any():
        poes = interpolate.interp1d(
            curve.losses, curve.poes,
            bounds_error=False, fill_value=0)(expected_curve.losses)
    else:
        poes = numpy.zeros(len(expected_curve.poes))

    return risk_almost_equal(poes, expected_curve.poes)


def getcursor(route):
    """Return a cursor from a Django route"""
    return connections[route].cursor()


def order_by_location(queryset):
    """
    Utility function to order a queryset by location. This works even if
    the location is of Geography object (a simple order_by('location') only
    works for Geometry objects).
    """
    return queryset.extra(
        select={'x': 'ST_X(geometry(location))',
                'y': 'ST_Y(geometry(location))'},
        order_by=["x", "y"])


def queryset_iter(queryset, chunk_size):
    """
    Given a QuerySet, split it into smaller queries and yield the result of
    each.

    :param queryset:
        A :class:`django.db.models.query.QuerySet` to iterate over, in chunks
        of ``chunk_size``.
    :param int chunksize:
        Chunk size for iteration over query results. For an unexecuted
        QuerySet, this will result in splitting a (potentially large) query
        into smaller queries.
    """
    offset = 0
    while True:
        chunk = list(queryset[offset:offset + chunk_size].iterator())
        if len(chunk) == 0:
            raise StopIteration
        else:
            yield chunk
            offset += chunk_size


def build_curves(rlz, curves_by_trt_model_gsim):
    """
    Build on the fly the hazard curves for the current realization
    by looking at the associations stored in the database table
    `hzrdr.assoc_lt_rlz_trt_model`.
    """
    # fixed a realization, there are T associations where T is the
    # number of TrtModels
    curves = 0
    for art in AssocLtRlzTrtModel.objects.filter(rlz=rlz):
        pnes = 1. - curves_by_trt_model_gsim[art.trt_model_id, art.gsim]
        curves = 1. - (1. - curves) * pnes
    return curves


## Tables in the 'hzrdi' (Hazard Input) schema.

class SiteModel(djm.Model):
    '''
    A model for site-specific parameters, used in hazard calculations.
    '''
    job = djm.ForeignKey('OqJob')
    # Average shear wave velocity for top 30 m. Units m/s.
    vs30 = djm.FloatField()
    # 'measured' or 'inferred'
    vs30_type = djm.TextField(choices=VS30_TYPE_CHOICES)
    # Depth to shear wave velocity of 1.0 km/s. Units m.
    z1pt0 = djm.FloatField()
    # Depth to shear wave velocity of 2.5 km/s. Units km.
    z2pt5 = djm.FloatField()
    location = djm.PointField(srid=DEFAULT_SRID)

    def __repr__(self):
        return (
            'SiteModel(location="%s", vs30=%s, vs30_type=%s, z1pt0=%s, '
            'z2pt5=%s)'
            % (self.location.wkt, self.vs30, self.vs30_type, self.z1pt0,
               self.z2pt5))

    class Meta:
        db_table = 'hzrdi\".\"site_model'


## Tables in the 'uiapi' schema.

class OqJob(djm.Model):
    '''
    An OpenQuake engine run started by the user
    '''
    user_name = djm.TextField()
    risk_calculation = djm.OneToOneField('RiskCalculation', null=True)
    LOG_LEVEL_CHOICES = (
        (u'debug', u'Debug'),
        (u'info', u'Info'),
        (u'progress', u'Progress'),
        (u'warn', u'Warn'),
        (u'error', u'Error'),
        (u'critical', u'Critical'),
    )
    log_level = djm.TextField(choices=LOG_LEVEL_CHOICES, default='progress')
    STATUS_CHOICES = (
        (u'pre_executing', u'Pre-Executing'),
        (u'executing', u'Executing'),
        (u'post_executing', u'Post-Executing'),
        (u'post_processing', u'Post-Processing'),
        (u'export', u'Exporting results'),
        (u'clean_up', u'Cleaning up'),
        (u'complete', u'Complete'),
    )
    status = djm.TextField(choices=STATUS_CHOICES, default='pre_executing')
    oq_version = djm.TextField(null=True, blank=True)
    hazardlib_version = djm.TextField(null=True, blank=True)
    nrml_version = djm.TextField(null=True, blank=True)
    risklib_version = djm.TextField(null=True, blank=True)
    is_running = djm.BooleanField(default=False)
    duration = djm.IntegerField(default=0)
    job_pid = djm.IntegerField(default=0)
    supervisor_pid = djm.IntegerField(default=0)
    last_update = djm.DateTimeField(editable=False, default=datetime.utcnow)

    class Meta:
        db_table = 'uiapi\".\"oq_job'

    @property
    def job_type(self):
        """
        'hazard' or 'risk'
        """
        return 'hazard' if self.risk_calculation is None else 'risk'

    @property
    def calculation(self):
        """
        :returns: a calculation object (hazard or risk) depending on
        the type of calculation. Useful in situations (e.g. core
        engine, stats, kvs, progress) where you do not have enough
        context about which kind of calculation is but still you want
        to access the common feature of a Calculation object.
        """
        return self.hazard_calculation or self.risk_calculation

    @property
    def hazard_calculation(self):
        return HazardCalculation(self) \
            if self.risk_calculation is None else None

    def get_param(self, name, missing=RAISE_EXC):
        """
        `job.get_param(name)` returns the value of the requested parameter
        or raise a MissingParameter exception if the parameter does not
        exist in the database.

        `job.get_param(name, missing)` returns the value of the requested
        parameter or the `missing` value if the parameter does not
        exist in the database.

        :param name: the name of the parameter
        :param missing: value returned if the parameter is missing

        NB: since job_param.value is NOT NULL, `.get_param(name)`
        can return None only if the parameter is missing.
        """
        try:
            return JobParam.objects.get(job=self, name=name).value
        except ObjectDoesNotExist:
            if missing is RAISE_EXC:
                raise MissingParameter(name)
            return missing

    def get_oqparam(self):
        """
        Return an OqParam object as read from the database
        """
        oqparam = object.__new__(OqParam)
        for row in JobParam.objects.filter(job=self):
            setattr(oqparam, row.name, row.value)
        return oqparam

    def save_params(self, params):
        """
        Save on the database table job_params the given parameters.

        :param job: an :class:`OqJob` instance
        :param params: a dictionary {name: string} of parameters
        """
        for name, value in params.iteritems():
            JobParam.objects.create(job=self, name=name, value=repr(value))

    def save_param(self, **name_vals):
        """
        Update parameters in JobParam, or create them, if needed
        """
        for name, value in name_vals.iteritems():
            jplist = list(JobParam.objects.filter(job=self, name=name))
            if jplist:  # update an existing parameter
                jplist[0].value = repr(value)
                jplist[0].save()
            else:  # add new parameter
                JobParam.objects.create(job=self, name=name, value=repr(value))

    def __repr__(self):
        return '<%s %d, %s>' % (self.__class__.__name__,
                                self.id, self.job_type)


class JobStats(djm.Model):
    '''
    Capture various statistics about a job.
    '''
    oq_job = djm.OneToOneField('OqJob')
    start_time = djm.DateTimeField(editable=False, default=datetime.utcnow)
    stop_time = djm.DateTimeField(editable=False)
    # The disk space occupation in bytes
    disk_space = djm.IntegerField(null=True)

    class Meta:
        db_table = 'uiapi\".\"job_stats'


# created at the end of the pre_execute phase
class JobInfo(djm.Model):
    '''
    Store information about a job.
    '''
    oq_job = djm.OneToOneField('OqJob')
    parent_job = djm.ForeignKey('OqJob')
    num_sites = djm.IntegerField(null=False)
    num_realizations = djm.IntegerField(null=False)
    num_imts = djm.IntegerField(null=False)
    num_levels = djm.FloatField(null=False)
    input_weight = djm.FloatField(null=False)
    output_weight = djm.FloatField(null=False)

    class Meta:
        db_table = 'uiapi\".\"job_info'


class JobParam(djm.Model):
    '''
    The parameters of a job
    '''
    job = djm.ForeignKey('OqJob')
    name = djm.TextField(null=False)
    value = fields.LiteralField(null=False)

    class Meta:
        db_table = 'uiapi\".\"job_param'


class Performance(djm.Model):
    '''
    Contains performance information about the operations performed by a task
    launched by a job.
    '''
    oq_job = djm.ForeignKey('OqJob')
    task_id = djm.TextField(null=True)
    task = djm.TextField(null=True)
    operation = djm.TextField(null=False)
    start_time = djm.DateTimeField(editable=False)
    duration = djm.FloatField(null=True)
    pymemory = djm.IntegerField(null=True)
    pgmemory = djm.IntegerField(null=True)

    class Meta:
        db_table = 'uiapi\".\"performance'


class HazardCalculation(object):
    '''
    Small wrapper around oqparam instances
    '''
    def __init__(self, job):
        if isinstance(job, (int, long, str)):  # passed the job_id
            self.oqjob = OqJob.objects.get(pk=job)
        else:
            self.oqjob = job
        params = vars(self.oqjob.get_oqparam())
        collisions = set(params) & set(dir(self))
        if collisions:
            raise NameError('The parameters %s will override attributes '
                            'of HazardCalculation instances' % collisions)
        vars(self).update(params)

    @property
    def vulnerability_models(self):
        return [self.inputs[vf_type]
                for vf_type in VULNERABILITY_TYPE_CHOICES
                if vf_type in self.inputs]

    def save_hazard_sites(self):
        """
        Populate the table HazardSite by inferring the points from
        the sites, region, or exposure.
        """
        if 'exposure' in self.inputs:
            assets = self.oqjob.exposuremodel.exposuredata_set.all()
            # the coords here must be sorted; the issue is that the
            # disaggregation calculator has a for loop of kind
            # for site in sites:
            #     bin_edge, disagg_matrix = disaggregation(site, ...)
            # the generated ruptures are random if the order of the sites
            # is random, even if the seed is fixed; in particular for some
            # ordering no ruptures are generated and the test
            # qa_tests/hazard/disagg/case_1/test.py fails with a bad
            # error message
            coords = sorted(
                set((asset.site.x, asset.site.y) for asset in assets))
            lons, lats = zip(*coords)
            mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
        else:
            mesh = get_mesh(self.oqjob.get_oqparam())
        sids = self.save_sites((p.longitude, p.latitude) for p in mesh)
        return mesh, sids

    def get_imts(self):
        """
        Returns intensity mesure types or intensity mesure types with levels
        in a fixed order.
        """
        imts = getattr(self, 'intensity_measure_types', None) or \
            self.intensity_measure_types_and_levels
        return sorted(imts)

    # this is used in the tests, see helpers.py
    def save_sites(self, coords):
        """
        Save all the gives sites on the hzrdi.hazard_site table.
        :param coordinates: a sequence of (lon, lat) pairs
        :returns: the ids of the inserted HazardSite instances
        """
        sites = [HazardSite(hazard_calculation=self.oqjob,
                            location='POINT(%s %s)' % (lon, lat))
                 for lon, lat in coords]
        return writer.CacheInserter.saveall(sites)

    def total_investigation_time(self):
        """
        Helper method to compute the total investigation time for a
        complete set of stochastic event sets for all realizations.
        """
        if self.number_of_logic_tree_samples > 0:
            # The calculation is set to do Monte-Carlo sampling of logic trees
            # The number of logic tree realizations is specified explicitly in
            # job configuration.
            n_lt_realizations = self.number_of_logic_tree_samples
        else:
            # The calculation is set do end-branch enumeration of all logic
            # tree paths
            # We can get the number of logic tree realizations by counting
            # initialized lt_realization records.
            n_lt_realizations = LtRealization.objects.filter(
                lt_model__hazard_calculation=self).count()

        investigation_time = (self.investigation_time
                              * self.ses_per_logic_tree_path
                              * n_lt_realizations)

        return investigation_time

    def gen_ruptures(self, sources, monitor, site_coll):
        """
        Yield (source, rupture, affected_sites) for each rupture
        generated by the given sources.

        :param sources: a sequence of sources
        :param monitor: a Monitor object
        """
        filtsources_mon = monitor.copy('filtering sources')
        genruptures_mon = monitor.copy('generating ruptures')
        filtruptures_mon = monitor.copy('filtering ruptures')
        for src in sources:
            with filtsources_mon:
                s_sites = src.filter_sites_by_distance_to_source(
                    self.maximum_distance, site_coll)
                if s_sites is None:
                    continue

            with genruptures_mon:
                ruptures = list(src.iter_ruptures())
            if not ruptures:
                continue

            for rupture in ruptures:
                with filtruptures_mon:
                    r_sites = filters.filter_sites_by_distance_to_rupture(
                        rupture, self.maximum_distance, s_sites)
                    if r_sites is None:
                        continue
                yield SourceRuptureSites(src, rupture, r_sites)
        filtsources_mon.flush()
        genruptures_mon.flush()
        filtruptures_mon.flush()

    def gen_ruptures_for_site(self, site, sources, monitor):
        """
        Yield source, <ruptures close to site>

        :param site: a Site object
        :param sources: a sequence of sources
        :param monitor: a Monitor object
        """
        source_rupture_sites = self.gen_ruptures(
            sources, monitor, SiteCollection([site]))
        for src, rows in itertools.groupby(
                source_rupture_sites, key=operator.attrgetter('source')):
            yield src, [row.rupture for row in rows]


class RiskCalculation(djm.Model):
    '''
    Parameters needed to run a Risk job.
    '''
    @classmethod
    def create(cls, **kw):
        return cls(**_prep_geometry(kw))

    #: Default maximum asset-hazard distance in km
    DEFAULT_MAXIMUM_DISTANCE = 5

    # Contains the absolute path to the directory containing the job config
    # file.
    base_path = djm.TextField()
    export_dir = djm.TextField(null=True, blank=True)

    #####################
    # General parameters:
    #####################

    # A description for this config profile which is meaningful to a user.
    description = djm.TextField(default='', blank=True)

    CALC_MODE_CHOICES = (
        (u'classical_risk', u'Classical PSHA'),
        (u'classical_bcr', u'Classical BCR'),
        (u'event_based_risk', u'Probabilistic Event-Based'),
        (u'event_based_fr', u'Event-Based From Ruptures'),
        (u'scenario_risk', u'Scenario'),
        (u'scenario_damage', u'Scenario Damage'),
        (u'event_based_bcr', u'Probabilistic Event-Based BCR'),
    )
    calculation_mode = djm.TextField(choices=CALC_MODE_CHOICES)
    inputs = fields.PickleField(blank=True)
    region_constraint = djm.PolygonField(
        srid=DEFAULT_SRID, null=True, blank=True)

    preloaded_exposure_model = djm.ForeignKey(
        'ExposureModel', null=True, blank=True)

    # the maximum distance for an hazard value with the corresponding
    # asset. Expressed in kilometers
    maximum_distance = djm.FloatField(
        null=True, blank=True, default=DEFAULT_MAXIMUM_DISTANCE)
    # the hazard output (it can point to an HazardCurvem, to a
    # Gmf or to a SES collection) used by the risk calculation
    hazard_output = djm.ForeignKey("Output", null=True, blank=True)

    # the HazardCalculation object used by the risk calculation when
    # each individual Output (i.e. each hazard logic tree realization)
    # is considered
    hazard_calculation = djm.ForeignKey("OqJob", null=True, blank=True)

    risk_investigation_time = djm.FloatField(
        help_text=('Override the time span (in years) with which the '
                   'hazard has been computed.'),
        null=True,
        blank=True,
    )

    # A seed used to generate random values to be applied to
    # vulnerability functions
    master_seed = djm.IntegerField(null=False, default=42)

    ####################################################
    # For calculators that output (conditional) loss map
    ####################################################
    conditional_loss_poes = fields.FloatArrayField(null=True, blank=True)

    ####################################################
    # For calculators that output statistical results
    ####################################################
    quantile_loss_curves = fields.FloatArrayField(
        help_text='List of quantiles for computing quantile outputs',
        null=True,
        blank=True)

    taxonomies_from_model = fields.OqNullBooleanField(
        help_text='if true calculation only consider the taxonomies in '
        'the fragility model', null=True, blank=True)

    ##################################
    # Probabilistic shared parameters
    ##################################
    # 0 == uncorrelated, 1 == perfect correlation by taxonomy
    asset_correlation = djm.FloatField(null=True, blank=True, default=0)

    #######################
    # Classical parameters:
    #######################
    lrem_steps_per_interval = djm.IntegerField(null=True, blank=True)

    poes_disagg = fields.FloatArrayField(
        null=True, blank=True,
        help_text='The probability of exceedance used to interpolate '
                  'loss curves for disaggregation purposes')

    #########################
    # Event-Based parameters:
    #########################
    loss_curve_resolution = djm.IntegerField(
        null=False, blank=True, default=DEFAULT_LOSS_CURVE_RESOLUTION)
    insured_losses = djm.NullBooleanField(null=True, blank=True, default=False)

    # The points of interest for disaggregation
    sites_disagg = djm.MultiPointField(
        srid=DEFAULT_SRID, null=True, blank=True)

    mag_bin_width = djm.FloatField(
        help_text=('Width of magnitude bins'),
        null=True,
        blank=True,
    )
    distance_bin_width = djm.FloatField(
        help_text=('Width of distance bins'),
        null=True,
        blank=True,
    )
    coordinate_bin_width = djm.FloatField(
        help_text=('Width of coordinate bins'),
        null=True,
        blank=True,
    )

    ######################################
    # BCR (Benefit-Cost Ratio) parameters:
    ######################################
    interest_rate = djm.FloatField(null=True, blank=True)
    asset_life_expectancy = djm.FloatField(null=True, blank=True)

    ######################################
    # Scenario parameters:
    ######################################
    time_event = fields.NullTextField()

    class Meta:
        db_table = 'uiapi\".\"risk_calculation'

    def get_hazard_calculation(self):
        """
        Get the hazard calculation associated with the hazard output used as an
        input to this risk calculation.

        :returns:
            :class:`HazardCalculation` instance.
        """
        try:
            hcalc = HazardCalculation(self.hazard_calculation or
                                      self.hazard_output.oq_job)
        except ObjectDoesNotExist:
            raise RuntimeError("The provided hazard does not exist")
        return hcalc

    def hazard_outputs(self):
        """
        Returns the list of hazard outputs to be considered. Apply
        `filters` to the default queryset
        """

        if self.hazard_output:
            return [self.hazard_output]
        elif self.hazard_calculation:
            if self.calculation_mode in ["classical_risk", "classical_bcr"]:
                filters = dict(output_type='hazard_curve_multi',
                               hazard_curve__lt_realization__isnull=False)
            elif self.calculation_mode in [
                    "event_based_risk", "event_based_bcr"]:
                filters = dict(
                    output_type='gmf', gmf__lt_realization__isnull=False)
            elif self.calculation_mode == "event_based_fr":
                filters = dict(output_type='ses')
            elif self.calculation_mode in ['scenario_risk', 'scenario_damage']:
                filters = dict(output_type='gmf_scenario')
            else:
                raise NotImplementedError

            return self.hazard_calculation.output_set.filter(
                **filters).order_by('id')
        else:
            raise RuntimeError("Neither hazard calculation "
                               "neither a hazard output has been provided")

    @property
    def best_maximum_distance(self):
        """
        Get the asset-hazard maximum distance (in km) to be used in
        hazard getters.

        :returns:
            The minimum between the maximum distance provided by the user (if
            not given, `DEFAULT_MAXIMUM_DISTANCE` is used as default) and the
            step (if exists) used by the hazard calculation.
        """
        dist = self.maximum_distance

        if dist is None:
            dist = self.DEFAULT_MAXIMUM_DISTANCE

        hc = self.get_hazard_calculation()
        if getattr(hc, 'region_grid_spacing', None) is not None:
            dist = min(dist, hc.region_grid_spacing * numpy.sqrt(2) / 2)

        # if we are computing hazard at exact location we set the
        # maximum_distance to a very small number in order to help the
        # query to find the results.
        if 'exposure' in hc.inputs:
            dist = 0.001
        return dist

    @property
    def is_bcr(self):
        return self.calculation_mode in ['classical_bcr', 'event_based_bcr']

    @property
    def exposure_model(self):
        """
        Return the right exposure model by following rules in order:

        1. if the `preloaded_exposure_model_id` is set in job_risk.ini, use it
        2. if an exposure_file is defined in job_risk.ini, use it
        3. if an exposure was used in the hazard job, use it
        4. if no exposure is found, return None
        """
        haz_job = self.get_hazard_calculation().oqjob
        return (self.preloaded_exposure_model or
                extract_from([self.oqjob, haz_job], 'exposuremodel'))

    @property
    def investigation_time(self):
        return (self.risk_investigation_time or
                self.get_hazard_calculation().investigation_time)


def extract_from(objlist, attr):
    """
    Extract an attribute from a list of Django objects, by scanning
    them in order until a not None attribute is found. If nothing is
    found, or if an exception ObjectDoesNotExist is raised, return None.

    :param objlist: the list of Django objects
    :param str attr: the name of the attribute to look for
    """
    for obj in objlist:
        try:
            value = getattr(obj, attr, None)
        except ObjectDoesNotExist:
            value = None
        if value is not None:
            return value


def _prep_geometry(kwargs):
    """
    Helper function to convert geometry specified in a job config file to WKT,
    so that it can save to the database in a geometry field.

    :param dict kwargs:
        keyword arguments, which may contain geometry definitions in
        a list form
    :returns:
        a dictionary with the geometries converted into WKT
    """
    kw = kwargs.copy()
    # If geometries were specified as string lists of coords,
    # convert them to WKT before doing anything else.
    for field, wkt_fmt in (('sites', 'MULTIPOINT(%s)'),
                           ('sites_disagg', 'MULTIPOINT(%s)'),
                           ('region', 'POLYGON((%s))'),
                           ('region_constraint', 'POLYGON((%s))')):
        coords = kwargs.get(field)
        if coords:  # construct WKT from the coords
            points = ['%s %s' % lon_lat for lon_lat in coords]
            # if this is the region, close the linear polygon
            # ring by appending the first coord to the end
            if field in ('region', 'region_constraint'):
                points.append(points[0])
            # update the field
            kw[field] = wkt_fmt % ', '.join(points)
    return kw


class Imt(djm.Model):
    """
    Table with the Intensity Measure Types
    """
    imt_str = djm.TextField(null=False)
    im_type = djm.TextField(choices=IMT_CHOICES)
    sa_period = djm.FloatField(null=True)
    sa_damping = djm.FloatField(null=True)
    stored_imts = None

    @classmethod
    def get(cls, imt_str):
        """
        :param imt_str: a string specifying the IMT
        :returns: a :class:`openquake.engine.db.models.Imt` instance
        """
        if cls.stored_imts is None:  # the first time
            cls.stored_imts = {imt.imt_str: imt
                               for imt in Imt.objects.filter()}
        return cls.stored_imts[imt_str]

    @classmethod
    def save_new(cls, hazardlib_imts):
        """
        Save the intensity measure types not already stored in the database.

        :param hazardlib_imts: a list of hazardlib IMT tuples
        """
        if cls.stored_imts is None:  # the first time
            cls.stored_imts = {imt.imt_str: imt
                               for imt in Imt.objects.filter()}
        for x in hazardlib_imts:
            imt_str = str(x)
            if imt_str not in cls.stored_imts:
                imt = cls.objects.create(
                    imt_str=imt_str,
                    im_type=x[0], sa_period=x[1], sa_damping=x[2])
                cls.stored_imts[imt_str] = imt

    class Meta:
        db_table = 'hzrdi\".\"imt'


class ImtTaxonomy(djm.Model):
    """
    Table with the associations IMT, taxonomy, as extracted from the risk
    models.
    """
    job = djm.ForeignKey('OqJob', null=False)
    imt = djm.ForeignKey('Imt', null=False)
    taxonomy = djm.TextField(null=True)

    class Meta:
        db_table = 'riski\".\"imt_taxonomy'


class OutputManager(djm.Manager):
    """
    Manager class to filter and create Output objects
    """
    def create_output(self, job, display_name, output_type):
        """
        Create an output for the given `job`, `display_name` and
        `output_type` (default to hazard_curve)
        """
        return self.create(oq_job=job,
                           display_name=display_name,
                           output_type=output_type)


class Output(djm.Model):
    '''
    A single artifact which is a result of an OpenQuake job.
    The data may reside in a file or in the database.
    '''

    #: Metadata of hazard outputs used by risk calculation. See
    #: `hazard_metadata` property for more details
    HazardMetadata = collections.namedtuple(
        'hazard_metadata',
        'investigation_time statistics quantile sm_path gsim_path')

    #: Hold the full paths in the model trees of ground shaking
    #: intensity models and of source models, respectively.
    LogicTreePath = collections.namedtuple(
        'logic_tree_path',
        'gsim_path sm_path')

    #: Hold the statistical params (statistics, quantile).
    StatisticalParams = collections.namedtuple(
        'statistical_params',
        'statistics quantile')

    oq_job = djm.ForeignKey('OqJob', null=False)

    display_name = djm.TextField()

    HAZARD_OUTPUT_TYPE_CHOICES = (
        (u'disagg_matrix', u'Disaggregation Matrix'),
        (u'gmf', u'Ground Motion Field'),
        (u'gmf_scenario', u'Ground Motion Field'),
        (u'hazard_curve', u'Hazard Curve'),
        (u'hazard_curve_multi', u'Hazard Curve (multiple imts)'),
        (u'hazard_map', u'Hazard Map'),
        (u'ses', u'Stochastic Event Set'),
        (u'uh_spectra', u'Uniform Hazard Spectra'),
    )

    RISK_OUTPUT_TYPE_CHOICES = (
        (u'agg_loss_curve', u'Aggregate Loss Curve'),
        (u'aggregate_loss', u'Aggregate Losses'),
        (u'bcr_distribution', u'Benefit-cost ratio distribution'),
        (u'collapse_map', u'Collapse Map Distribution'),
        (u'dmg_dist_per_asset', u'Damage Distribution Per Asset'),
        (u'dmg_dist_per_taxonomy', u'Damage Distribution Per Taxonomy'),
        (u'dmg_dist_total', u'Total Damage Distribution'),
        (u'event_loss', u'Event Loss Table'),
        (u'loss_curve', u'Loss Curve'),
        (u'event_loss_curve', u'Loss Curve'),
        (u'loss_fraction', u'Loss fractions'),
        (u'loss_map', u'Loss Map'),
    )

    output_type = djm.TextField(
        choices=HAZARD_OUTPUT_TYPE_CHOICES + RISK_OUTPUT_TYPE_CHOICES)
    last_update = djm.DateTimeField(editable=False, default=datetime.utcnow)

    objects = OutputManager()

    def __str__(self):
        return "%d||%s||%s" % (self.id, self.output_type, self.display_name)

    class Meta:
        db_table = 'uiapi\".\"output'
        ordering = ['id']

    def is_hazard_curve(self):
        return self.output_type in ['hazard_curve', 'hazard_curve_multi']

    @property
    def output_container(self):
        """
        :returns: the output container associated with this output
        """

        # FIXME(lp). Remove the following outstanding exceptions
        if self.output_type in ['agg_loss_curve', 'event_loss_curve']:
            return self.loss_curve
        elif self.output_type == 'hazard_curve_multi':
            return self.hazard_curve
        elif self.output_type == 'gmf_scenario':
            return self.gmf
        return getattr(self, self.output_type)

    @property
    def lt_realization_paths(self):
        """
        :returns: an instance of `LogicTreePath` the output is
        associated with. When the output is not associated with any
        logic tree branch then it returns a LogicTreePath namedtuple
        with a couple of None.
        """
        hazard_output_types = [el[0] for el in self.HAZARD_OUTPUT_TYPE_CHOICES]
        risk_output_types = [el[0] for el in self.RISK_OUTPUT_TYPE_CHOICES]
        container = self.output_container

        if self.output_type in hazard_output_types:
            rlz = getattr(container, 'lt_realization_id', None)
            if rlz is not None:
                return self.LogicTreePath(
                    tuple(container.lt_realization.gsim_lt_path),
                    tuple(container.lt_realization.sm_lt_path))
            else:
                return self.LogicTreePath(None, None)
        elif self.output_type in risk_output_types:
            if getattr(container, 'hazard_output_id', None):
                return container.hazard_output.lt_realization_paths
            else:
                return self.LogicTreePath(None, None)

        raise RuntimeError("unexpected output type %s" % self.output_type)

    @property
    def statistical_params(self):
        """
        :returns: an instance of `StatisticalParams` the output is
        associated with
        """
        if getattr(self.output_container, 'statistics', None) is not None:
            return self.StatisticalParams(self.output_container.statistics,
                                          self.output_container.quantile)
        elif getattr(
                self.output_container, 'hazard_output_id', None) is not None:
            return self.output_container.hazard_output.statistical_params
        else:
            return self.StatisticalParams(None, None)

    @property
    def hazard_metadata(self):
        """
        Given an Output produced by a risk calculation it returns the
        corresponding hazard metadata.

        :returns:
            A `namedtuple` with the following attributes::

                * investigation_time: the hazard investigation time (float)
                * statistics: the kind of hazard statistics (None, "mean" or
                  "quantile")
                * quantile: quantile value (when `statistics` is "quantile")
                * sm_path: a list representing the source model path
                * gsim_path: a list representing the gsim logic tree path

        """
        investigation_time = self.oq_job\
                                 .risk_calculation\
                                 .hazard_calculation\
                                 .get_param('investigation_time', None)

        statistics, quantile = self.statistical_params
        gsim_lt_path, sm_lt_path = self.lt_realization_paths

        return self.HazardMetadata(investigation_time,
                                   statistics, quantile,
                                   sm_lt_path, gsim_lt_path)


## Tables in the 'hzrdr' schema.


class HazardMap(djm.Model):
    '''
    Hazard Map header (information which pertains to entire map)
    '''
    output = djm.OneToOneField('Output', related_name="hazard_map")
    # FK only required for non-statistical results (i.e., mean or quantile
    # curves).
    lt_realization = djm.ForeignKey('LtRealization', null=True)
    investigation_time = djm.FloatField()
    imt = djm.TextField(choices=IMT_CHOICES)
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)
    sa_period = djm.FloatField(null=True)
    sa_damping = djm.FloatField(null=True)
    poe = djm.FloatField()
    # lons, lats, and imls are stored as numpy arrays with a uniform size and
    # shape
    lons = fields.FloatArrayField()
    lats = fields.FloatArrayField()
    imls = fields.FloatArrayField()

    class Meta:
        db_table = 'hzrdr\".\"hazard_map'

    def __str__(self):
        return (
            'HazardMap(poe=%(poe)s, imt=%(imt)s, sa_period=%(sa_period)s, '
            'statistics=%(statistics)s, quantile=%(quantile)s)'
        ) % self.__dict__

    def __repr__(self):
        return self.__str__()


class HazardCurve(djm.Model):
    '''
    Hazard Curve header information
    '''
    output = djm.OneToOneField(
        'Output', null=True, related_name="hazard_curve")
    # FK only required for non-statistical results (i.e., mean or quantile
    # curves).
    lt_realization = djm.ForeignKey('LtRealization', null=True)
    investigation_time = djm.FloatField()
    imt = djm.TextField(choices=IMT_CHOICES, default=None, blank=True)
    imls = fields.FloatArrayField()
    STAT_CHOICES = (
        (u'mean', u'Mean'),
        (u'quantile', u'Quantile'),
    )
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)
    sa_period = djm.FloatField(null=True)
    sa_damping = djm.FloatField(null=True)

    class Meta:
        db_table = 'hzrdr\".\"hazard_curve'

    @property
    def imt_long(self):
        """
        :returns: a string representing the imt associated with the
        curve (if any) in the long form, e.g. SA(0.01)
        """
        if self.imt:
            if self.imt == "SA":
                return "%s(%s)" % (self.imt, self.sa_damping)
            else:
                return self.imt

    def __iter__(self):
        assert self.output.output_type == 'hazard_curve_multi'

        siblings = self.__class__.objects.filter(
            output__oq_job=self.output.oq_job,
            output__output_type='hazard_curve')

        if not self.statistics:
            return iter(siblings.filter(lt_realization__isnull=False))
        elif self.quantile:
            return iter(
                siblings.filter(statistics="quantile", quantile=self.quantile))
        else:
            return iter(siblings.filter(statistics="mean"))


class HazardCurveDataManager(djm.GeoManager):
    """
    Manager class to filter and create HazardCurveData objects
    """

    def all_curves_for_imt(self, job, imt, sa_period, sa_damping):
        """
        Helper function for creating a :class:`django.db.models.query.QuerySet`
        for selecting all curves from all realizations for a given ``job_id``
        and ``imt``.

        :param job:
            An :class:`openquake.engine.db.models.OqJob` instance.
        :param str imt:
            Intensity measure type.
        :param sa_period:
            Spectral Acceleration period value. Only relevant if the ``imt`` is
            "SA".
        :param sa_damping:
            Spectrail Acceleration damping value. Only relevant if the ``imt``
            is "SA".
        """
        return self.filter(hazard_curve__output__oq_job=job,
                           hazard_curve__imt=imt,
                           hazard_curve__sa_period=sa_period,
                           hazard_curve__sa_damping=sa_damping,
                           # We only want curves associated with a logic tree
                           # realization (and not statistical aggregates):
                           hazard_curve__lt_realization__isnull=False)

    def all_curves_simple(self, filter_args=None, order_by='id'):
        """
        Get all :class:`HazardCurveData` records matching `filter_args` and
        return the results in a simple, lean format: a sequence of (x, y, poes)
        triples, where x and y are longitude and latitude of the `location`.

        For querying large sets of hazard curve data, this is a rather lean
        and efficient method for getting the results.

        :param dict filter_args:
            Optional. Dictionary of filter arguments to apply to the query.
        :param str order_by:
            Defaults to the primary key ('id'). Field by which to order
            results. Currently, only one `ORDER BY` field is supported.
        """
        if filter_args is None:
            filter_args = dict()

        return self\
            .filter(**filter_args)\
            .order_by(order_by)\
            .extra(select={'x': 'ST_X(location)', 'y': 'ST_Y(location)'})\
            .values_list('x', 'y', 'poes')\
            .iterator()


class HazardCurveData(djm.Model):
    '''
    Hazard Curve data

    Contains an list of PoE (Probability of Exceedance)
    values and the geographical point associated with the curve
    '''
    hazard_curve = djm.ForeignKey('HazardCurve')
    poes = fields.FloatArrayField()
    location = djm.PointField(srid=DEFAULT_SRID)
    # weight can be null/None if the weight is implicit:
    weight = djm.DecimalField(decimal_places=100, max_digits=101, null=True)

    objects = HazardCurveDataManager()

    class Meta:
        db_table = 'hzrdr\".\"hazard_curve_data'


class SESCollection(djm.Model):
    """
    Stochastic Event Set Collection: A container for 1 or more Stochastic Event
    Sets for a given logic tree realization.

    See also :class:`SES` and :class:`SESRupture`.
    """
    output = djm.OneToOneField('Output', related_name="ses")
    lt_model = djm.OneToOneField(
        'LtSourceModel', related_name='ses_collection', null=True)
    ordinal = djm.IntegerField(null=False)

    class Meta:
        db_table = 'hzrdr\".\"ses_collection'
        ordering = ['ordinal']

    @property
    def sm_lt_path(self):
        """
        The source model logic tree path corresponding to the collection
        """
        if self.lt_model is None:  # scenario
            return ()
        return tuple(self.lt_model.sm_lt_path)

    def get_ruptures(self):
        """Return the SESRuptures associated to self"""
        return SESRupture.objects.filter(rupture__ses_collection=self.id)

    def __iter__(self):
        """
        Iterator for walking through all child :class:`SES` objects.
        """
        hc = self.output.oq_job.hazard_calculation
        n = getattr(hc, 'ses_per_logic_tree_path', 1)  # 1 for scenario
        for ordinal in xrange(1, n + 1):
            yield SES(self, ordinal)

    def __len__(self):
        """
        Return the ses_per_logic_tree_path parameter
        """
        return self.output.oq_job.hazard_calculation.ses_per_logic_tree_path

    def __repr__(self):
        return '<%s=%d, lt_model=%s, ordinal=%d>' % (
            self.__class__.__name__, self.id, self.lt_model.id, self.ordinal)


class SES(object):
    """
    Stochastic Event Set: A container for 1 or more ruptures associated with a
    specific investigation time span.
    """
    # the ordinal must be > 0: the reason is that it appears in the
    # exported XML file and the schema constraints the number to be
    # nonzero
    def __init__(self, ses_collection, ordinal=1):
        self.ses_collection = ses_collection
        self.ordinal = ordinal
        self.investigation_time = self.ses_collection.output.oq_job.get_param(
            'investigation_time', None)

    def __cmp__(self, other):
        return cmp(self.ordinal, other.ordinal)

    def __iter__(self):
        """
        Iterator for walking through all child :class:`SESRupture` objects.
        """
        return SESRupture.objects.filter(
            rupture__ses_collection=self.ses_collection.id,
            ses_id=self.ordinal).order_by('tag').iterator()


def is_from_fault_source(rupture):
    """
    If True, this rupture was generated from a simple/complex fault
    source. If False, this rupture was generated from a point/area source.

    :param rupture: an instance of :class:
    `openquake.hazardlib.source.rupture.BaseProbabilisticRupture`
    """
    typology = rupture.source_typology
    is_char = typology is source.CharacteristicFaultSource
    is_complex_or_simple = typology in (
        source.ComplexFaultSource,
        source.SimpleFaultSource)
    is_complex_or_simple_surface = isinstance(
        rupture.surface, (geo.ComplexFaultSurface,
                          geo.SimpleFaultSurface))
    return is_complex_or_simple or (
        is_char and is_complex_or_simple_surface)


def is_multi_surface(rupture):
    """
    :param rupture: an instance of :class:
    `openquake.hazardlib.source.rupture.BaseProbabilisticRupture`

    :returns: a boolean
    """
    typology = rupture.source_typology
    is_char = typology is source.CharacteristicFaultSource
    is_multi_sur = isinstance(rupture.surface, geo.MultiSurface)
    return is_char and is_multi_sur


def get_geom(surface, is_from_fault_source, is_multi_surface):
    """
    The following fields can be interpreted different ways,
    depending on the value of `is_from_fault_source`. If
    `is_from_fault_source` is True, each of these fields should
    contain a 2D numpy array (all of the same shape). Each triple
    of (lon, lat, depth) for a given index represents the node of
    a rectangular mesh. If `is_from_fault_source` is False, each
    of these fields should contain a sequence (tuple, list, or
    numpy array, for example) of 4 values. In order, the triples
    of (lon, lat, depth) represent top left, top right, bottom
    left, and bottom right corners of the the rupture's planar
    surface. Update: There is now a third case. If the rupture
    originated from a characteristic fault source with a
    multi-planar-surface geometry, `lons`, `lats`, and `depths`
    will contain one or more sets of 4 points, similar to how
    planar surface geometry is stored (see above).

    :param rupture: an instance of :class:
    `openquake.hazardlib.source.rupture.BaseProbabilisticRupture`

    :param is_from_fault_source: a boolean
    :param is_multi_surface: a boolean
    """
    if is_from_fault_source:
        # for simple and complex fault sources,
        # rupture surface geometry is represented by a mesh
        surf_mesh = surface.get_mesh()
        lons = surf_mesh.lons
        lats = surf_mesh.lats
        depths = surf_mesh.depths
    else:
        if is_multi_surface:
            # `list` of
            # openquake.hazardlib.geo.surface.planar.PlanarSurface
            # objects:
            surfaces = surface.surfaces

            # lons, lats, and depths are arrays with len == 4*N,
            # where N is the number of surfaces in the
            # multisurface for each `corner_*`, the ordering is:
            #   - top left
            #   - top right
            #   - bottom left
            #   - bottom right
            lons = numpy.concatenate([x.corner_lons for x in surfaces])
            lats = numpy.concatenate([x.corner_lats for x in surfaces])
            depths = numpy.concatenate([x.corner_depths for x in surfaces])
        else:
            # For area or point source,
            # rupture geometry is represented by a planar surface,
            # defined by 3D corner points
            lons = numpy.zeros((4))
            lats = numpy.zeros((4))
            depths = numpy.zeros((4))

            # NOTE: It is important to maintain the order of these
            # corner points. TODO: check the ordering
            for i, corner in enumerate((surface.top_left,
                                        surface.top_right,
                                        surface.bottom_left,
                                        surface.bottom_right)):
                lons[i] = corner.longitude
                lats[i] = corner.latitude
                depths[i] = corner.depth
    return lons, lats, depths


class ProbabilisticRupture(djm.Model):
    """
    A rupture as part of a Stochastic Event Set Collection.
    """
    ses_collection = djm.ForeignKey('SESCollection')
    magnitude = djm.FloatField(null=False)
    _hypocenter = fields.FloatArrayField(null=False)
    rake = djm.FloatField(null=False)
    trt_model = djm.ForeignKey('TrtModel')
    is_from_fault_source = djm.NullBooleanField(null=False)
    is_multi_surface = djm.NullBooleanField(null=False)
    surface = fields.PickleField(null=False)
    site_indices = fields.IntArrayField(null=True)

    # NB (MS): the proper solution would be to store the hypocenter as a 3D
    # point, however I was unable to do so, due to a bug in Django 1.3
    # (I was getting a GeometryProxy exception).
    # The GEOS library we are using does not even support
    # the WKT for 3D points; that's why I am storing the point as a
    # 3D array, as a workaround; luckily, we never perform any
    # geospatial query on the hypocenter.
    # we will be able to do better when we will upgrade (Geo)Django
    @property
    def hypocenter(self):
        """Convert the 3D array into a hazardlib point"""
        return geo.Point(*self._hypocenter)

    class Meta:
        db_table = 'hzrdr\".\"probabilistic_rupture'

    @classmethod
    def create(cls, rupture, ses_collection, trt_model, site_indices=None):
        """
        Create a ProbabilisticRupture row on the database.

        :param rupture:
            a hazardlib rupture
        :param ses_collection:
            a Stochastic Event Set Collection object
        :param trt_model:
            the TRTModel generating the rupture
        :param site_indices:
            an array of indices for the site_collection
        """
        iffs = is_from_fault_source(rupture)
        ims = is_multi_surface(rupture)
        lons, lats, depths = get_geom(rupture.surface, iffs, ims)
        hp = rupture.hypocenter
        return cls.objects.create(
            ses_collection=ses_collection,
            magnitude=rupture.mag,
            rake=rupture.rake,
            trt_model=trt_model,
            is_from_fault_source=iffs,
            is_multi_surface=ims,
            surface=rupture.surface,
            _hypocenter=[hp.longitude, hp.latitude, hp.depth],
            site_indices=site_indices)

    @property
    def tectonic_region_type(self):
        """The TRT associated to the underlying trt_model"""
        return self.trt_model.tectonic_region_type

    _geom = None

    @property
    def geom(self):
        """
        Extract the triple (lons, lats, depths) from the surface geometry
        (cached).
        """
        if self._geom is not None:
            return self._geom
        self._geom = get_geom(self.surface, self.is_from_fault_source,
                              self.is_multi_surface)
        return self._geom

    @property
    def lons(self):
        return self.geom[0]

    @property
    def lats(self):
        return self.geom[1]

    @property
    def depths(self):
        return self.geom[2]

    @property
    def strike(self):
        return self.surface.get_strike()

    @property
    def dip(self):
        return self.surface.get_dip()

    @property
    def mag(self):
        return self.magnitude

    def _validate_planar_surface(self):
        """
        A rupture's planar surface (existing only in the case of ruptures from
        area/point sources) may only consist of 4 points (top left, top right,
        bottom right, and bottom left corners, in that order).

        If the surface is not valid, a :exc:`ValueError` is raised.

        This should only be used if `is_from_fault_source` is `False`.
        """
        if not (4 == len(self.lons) == len(self.lats) == len(self.depths)):
            raise ValueError(
                "Incorrect number of points; there should be exactly 4")

    @property
    def top_left_corner(self):
        if not (self.is_from_fault_source or self.is_multi_surface):
            self._validate_planar_surface()
            return self.lons[0], self.lats[0], self.depths[0]
        return None

    @property
    def top_right_corner(self):
        if not (self.is_from_fault_source or self.is_multi_surface):
            self._validate_planar_surface()
            return self.lons[1], self.lats[1], self.depths[1]
        return None

    @property
    def bottom_left_corner(self):
        if not (self.is_from_fault_source or self.is_multi_surface):
            self._validate_planar_surface()
            return self.lons[2], self.lats[2], self.depths[2]
        return None

    @property
    def bottom_right_corner(self):
        if not (self.is_from_fault_source or self.is_multi_surface):
            self._validate_planar_surface()
            return self.lons[3], self.lats[3], self.depths[3]
        return None


class SESRupture(djm.Model):
    """
    A rupture as part of a Stochastic Event Set.
    """
    rupture = djm.ForeignKey('ProbabilisticRupture')
    ses_id = djm.IntegerField(null=False)
    tag = djm.TextField(null=False)
    seed = djm.IntegerField(null=False)

    class Meta:
        db_table = 'hzrdr\".\"ses_rupture'
        ordering = ['tag']

    @classmethod
    def create(cls, prob_rupture, ses_ordinal, source_id, rupt_no, rupt_occ,
               seed):
        """
        Create a SESRupture row in the database.

        :param prob_rupture:
            :class:`openquake.engine.db.models.ProbabilisticRupture` instance
        :param int ses_ordinal:
            ordinal for a :class:`openquake.engine.db.models.SES` instance
        :param str source_id:
            id of the source that generated the rupture
        :param rupt_no:
            the rupture number (an ordinal from source.iter_ruptures())
        :param rupt_occ:
            the occurrence number of the rupture in the given ses
        :param int seed:
            a seed that will be used when computing the GMF from the rupture
        """
        tag = 'smlt=%02d|ses=%04d|src=%s|rup=%03d-%02d' % (
            prob_rupture.ses_collection.ordinal, ses_ordinal,
            source_id, rupt_no, rupt_occ)
        return cls.objects.create(
            rupture=prob_rupture, ses_id=ses_ordinal, tag=tag, seed=seed)

    @property
    def surface(self):
        """The surface of the underlying rupture"""
        return self.rupture.surface

    @property
    def hypocenter(self):
        """The hypocenter of the underlying rupture"""
        return self.rupture.hypocenter


_Point = collections.namedtuple('_Point', 'x y')


def get_correl_model(job):
    """
    Helper function for constructing the appropriate correlation model.

    :returns:
        A correlation object. See :mod:`openquake.hazardlib.correlation`
        for more info.
    """
    correl_model_name = job.get_param('ground_motion_correlation_model', None)
    if correl_model_name is None:
        # There's no correlation model for this calculation.
        return None
    correl_model_cls = getattr(
        correlation, '%sCorrelationModel' % correl_model_name, None)
    if correl_model_cls is None:
        # There's no correlation model for this calculation.
        return None
    gmc_params = job.get_param('ground_motion_correlation_params', None)
    return correl_model_cls(**gmc_params)


class Gmf(djm.Model):
    """
    A collection of ground motion field (GMF) sets for a given logic tree
    realization.
    """
    output = djm.OneToOneField('Output', related_name="gmf")
    lt_realization = djm.ForeignKey('LtRealization', null=False)

    class Meta:
        db_table = 'hzrdr\".\"gmf'

    def by_rupture(self, sitecol, ses_collection_id=None, ses_ordinal=None):
        """
        Yields triples (ses_rupture, sites, gmf_dict)
        """
        job = self.output.oq_job
        hc = job.hazard_calculation
        correl_model = get_correl_model(job)
        gsims = self.lt_realization.get_gsim_instances()
        assert gsims, 'No GSIMs found for realization %d!' % \
            self.lt_realization.id  # look into hzdr.assoc_lt_rlz_trt_model
        # NB: the IMTs must be sorted for consistency with the classical
        # calculator when computing the hazard curves from the GMFs
        imts = map(from_string, sorted(hc.intensity_measure_types))
        for ses_coll in SESCollection.objects.filter(
                output__oq_job=self.output.oq_job):
            # filter by ses_collection
            if ses_collection_id and ses_collection_id != ses_coll.id:
                continue
            for ses in ses_coll:
                # filter by ses_ordinal
                if ses_ordinal and ses_ordinal != ses.ordinal:
                    continue
                for rupture, ses_ruptures in itertools.groupby(
                        ses, operator.attrgetter('rupture')):
                    sites = sitecol if rupture.site_indices is None\
                        else FilteredSiteCollection(
                            rupture.site_indices, sitecol)
                    computer = calc.gmf.GmfComputer(
                        rupture, sites, imts, gsims,
                        getattr(hc, 'truncation_level', None), correl_model)
                    for ses_rup in ses_ruptures:
                        yield ses_rup, sites, computer.compute(ses_rup.seed)

    # this method in the future will replace __iter__, by enabling
    # GMF-export by recomputation
    def iternew(self):
        """
        Get the ground motion fields per SES ("GMF set") for
        the XML export. Each "GMF set" should:

            * have an `investigation_time` attribute
            * have an `stochastic_event_set_id` attribute
            * be iterable, yielding a sequence of "GMF" objects

            Each "GMF" object should:

            * have an `imt` attribute
            * have an `sa_period` attribute (only if `imt` is 'SA')
            * have an `sa_damping` attribute (only if `imt` is 'SA')
            * have a `rupture_id` attribute (to indicate which rupture
              contributed to this gmf)
            * be iterable, yielding a sequence of "GMF node" objects

            Each "GMF node" object should have:

            * a `gmv` attribute (to indicate the ground motion value
            * `lon` and `lat` attributes (to indicate the geographical location
              of the ground motion field)

        If a SES does not generate any GMF, it is ignored.
        """
        for ses_coll in SESCollection.objects.filter(
                output__oq_job=self.output.oq_job):
            for ses in ses_coll:
                gmfset = []  # set of GMFs generate by the same SES
                for ses_rup, sites, gmf_dict in self.by_rupture(
                        ses_coll.id, ses.ordinal):
                    for gsim, imt in gmf_dict:
                        gmvs = gmf_dict[gsim, imt]
                        im_type, sa_period, sa_damping = imt
                            # using a generator here saves a lot of memory
                        nodes = (_GroundMotionFieldNode(gmv, _Point(x, y))
                                 for gmv, x, y in
                                 zip(gmvs, sites.lons, sites.lats))
                        gmfset.append(
                            _GroundMotionField(
                                im_type, sa_period, sa_damping,
                                ses_rup.tag, nodes))
                if gmfset:
                    yield GmfSet(ses, gmfset)

    def __iter__(self):
        """
        Get the ground motion fields per SES ("GMF set") for
        the XML export. Each "GMF set" should:

            * have an `investigation_time` attribute
            * have an `stochastic_event_set_id` attribute
            * be iterable, yielding a sequence of "GMF" objects

            Each "GMF" object should:

            * have an `imt` attribute
            * have an `sa_period` attribute (only if `imt` is 'SA')
            * have an `sa_damping` attribute (only if `imt` is 'SA')
            * have a `rupture_id` attribute (to indicate which rupture
              contributed to this gmf)
            * be iterable, yielding a sequence of "GMF node" objects

            Each "GMF node" object should have:

            * a `gmv` attribute (to indicate the ground motion value
            * `lon` and `lat` attributes (to indicate the geographical location
              of the ground motion field)

        If a SES does not generate any GMF, it is ignored.
        """
        job = self.output.oq_job
        for ses_coll in SESCollection.objects.filter(output__oq_job=job):
            for ses in ses_coll:
                query = """\
        SELECT imt, sa_period, sa_damping, tag,
               array_agg(gmv) AS gmvs,
               array_agg(ST_X(location::geometry)) AS xs,
               array_agg(ST_Y(location::geometry)) AS ys
        FROM (SELECT imt, sa_period, sa_damping,
             unnest(rupture_ids) as rupture_id, location, unnest(gmvs) AS gmv
           FROM hzrdr.gmf_data, hzrdi.hazard_site
            WHERE site_id = hzrdi.hazard_site.id AND hazard_calculation_id=%s
           AND gmf_id=%d) AS x, hzrdr.ses_rupture AS y,
           hzrdr.probabilistic_rupture AS z
        WHERE x.rupture_id = y.id AND y.rupture_id=z.id
        AND y.ses_id=%d AND z.ses_collection_id=%d
        GROUP BY imt, sa_period, sa_damping, tag
        ORDER BY imt, sa_period, sa_damping, tag;
        """ % (job.id, self.id, ses.ordinal, ses_coll.id)
                curs = getcursor('job_init')
                curs.execute(query)
                # a set of GMFs generate by the same SES, one per rupture
                gmfset = []
                for (imt, sa_period, sa_damping, rupture_tag, gmvs,
                     xs, ys) in curs:
                    # using a generator here saves a lot of memory
                    nodes = (_GroundMotionFieldNode(gmv, _Point(x, y))
                             for gmv, x, y in zip(gmvs, xs, ys))
                    gmfset.append(
                        _GroundMotionField(
                            imt, sa_period, sa_damping, rupture_tag,
                            nodes))
                if gmfset:
                    yield GmfSet(ses, gmfset)


class GmfSet(object):
    """
    Small wrapper around the list of Gmf objects associated to the given SES.
    """
    def __init__(self, ses, gmfset):
        self.ses = ses
        self.gmfset = gmfset
        self.investigation_time = ses.investigation_time
        self.stochastic_event_set_id = ses.ordinal

    def __iter__(self):
        return iter(self.gmfset)

    def __str__(self):
        return (
            'GMFsPerSES(investigation_time=%f, '
            'stochastic_event_set_id=%s,\n%s)' % (
                self.ses.investigation_time,
                self.ses.ordinal, '\n'.join(
                    sorted(str(g) for g in self.gmfset))))


class _GroundMotionField(object):
    """
    The Ground Motion Field generated by the given rupture
    """
    def __init__(self, imt, sa_period, sa_damping, rupture_id, gmf_nodes):
        self.imt = imt
        self.sa_period = sa_period
        self.sa_damping = sa_damping
        self.rupture_id = rupture_id
        self.gmf_nodes = gmf_nodes

    def __iter__(self):
        return iter(self.gmf_nodes)

    def __getitem__(self, key):
        return self.gmf_nodes[key]

    def __str__(self):
        """
        String representation of a _GroundMotionField object showing the
        content of the nodes (lon, lat an gmv). This is useful for debugging
        and testing.
        """
        mdata = ('imt=%(imt)s sa_period=%(sa_period)s '
                 'sa_damping=%(sa_damping)s rupture_id=%(rupture_id)s' %
                 vars(self))
        nodes = sorted(map(str, self.gmf_nodes))
        return 'GMF(%s\n%s)' % (mdata, '\n'.join(nodes))


class _GroundMotionFieldNode(object):

    # the signature is not (gmv, x, y) because the XML writer expect a location
    # object
    def __init__(self, gmv, loc):
        self.gmv = gmv
        self.location = loc

    def __lt__(self, other):
        """
        A reproducible ordering by lon and lat; used in
        :function:`openquake.nrmllib.hazard.writers.gen_gmfs`
        """
        return self.location < other.location

    def __str__(self):
        """Return lon, lat and gmv of the node in a compact string form"""
        return '<X=%9.5f, Y=%9.5f, GMV=%9.7f>' % (
            self.location.x, self.location.y, self.gmv)


class GmfData(djm.Model):
    """
    Ground Motion Field: A collection of ground motion values and their
    respective geographical locations.
    """
    gmf = djm.ForeignKey('Gmf')
    task_no = djm.IntegerField(null=False)
    imt = djm.TextField(choices=IMT_CHOICES)
    sa_period = djm.FloatField(null=True)
    sa_damping = djm.FloatField(null=True)
    gmvs = fields.FloatArrayField()
    rupture_ids = fields.IntArrayField(null=True)
    site = djm.ForeignKey('HazardSite')
    objects = djm.GeoManager()

    class Meta:
        db_table = 'hzrdr\".\"gmf_data'
        ordering = ['gmf', 'task_no']


# XXX: we should order the gmvs consistently
# currently the scenario export test works by accident
def _get_gmf(curs, gmf_id, imtype, sa_period, sa_damping):
    # returns site_id, gmvs for the given gmf_id and imt
    query = '''\
    SELECT site_id, array_concat(gmvs), array_concat(rupture_ids)
    FROM hzrdr.gmf_data WHERE gmf_id=%s AND imt=%s {}
    GROUP BY site_id ORDER BY site_id'''
    if imtype == 'SA':
        curs.execute(query.format('AND sa_period=%s AND sa_damping=%s'),
                     (gmf_id, imtype, sa_period, sa_damping))
    else:
        curs.execute(query.format(''), (gmf_id, imtype))
    for site_id, gmvs, rupture_ids in curs:
        gmv = dict(zip(rupture_ids, gmvs))
        yield site_id, [gmv[r] for r in sorted(rupture_ids)]


# used in the scenario QA tests
def get_gmvs_per_site(output, imt):
    """
    Iterator for walking through all :class:`GmfData` objects associated
    to a given output. Notice that values for the same site are
    displayed together and ordered according to the rupture ids, so that
    it is possible to get consistent outputs in the test cases.

    :param output: instance of :class:`openquake.engine.db.models.Output`

    :param string imt: a string with the IMT to extract

    :returns: a list of ground motion values per each site
    """
    curs = getcursor('job_init')
    for site_id, gmvs in _get_gmf(curs, output.gmf.id, *from_string(imt)):
        yield gmvs


# used in the scenario export
def get_gmfs_scenario(output, imt=None):
    """
    Iterator for walking through all :class:`GmfData` objects associated
    to a given output. Notice that the fields are ordered according to the
    location, so it is possible to get reproducible outputs in the test cases.

    :param output: instance of :class:`openquake.engine.db.models.Output`

    :param string imt: a string with the IMT to extract; the default
                       is None, all the IMT in the job.ini file are extracted

    :returns: an iterator over
              :class:`openquake.engine.db.models._GroundMotionField` instances
    """
    hc = output.oq_job.hazard_calculation
    if imt is None:
        imts = distinct(from_string(x) for x in hc.get_imts())
    else:
        imts = [from_string(imt)]
    curs = getcursor('job_init')
    for imt, sa_period, sa_damping in imts:
        nodes = collections.defaultdict(list)  # realization -> gmf_nodes
        for site_id, gmvs in _get_gmf(
                curs, output.gmf.id, imt, sa_period, sa_damping):
            for i, gmv in enumerate(gmvs):  # i is the realization index
                site = HazardSite.objects.get(pk=site_id)
                nodes[i].append(_GroundMotionFieldNode(gmv, site.location))
        for gmf_nodes in nodes.itervalues():
            yield _GroundMotionField(
                imt=imt,
                sa_period=sa_period,
                sa_damping=sa_damping,
                rupture_id=None,
                gmf_nodes=sorted(gmf_nodes, key=operator.attrgetter('gmv')))


class DisaggResult(djm.Model):
    """
    Storage for disaggregation historgrams. Each histogram is stored in
    `matrix` as a 6-dimensional numpy array (pickled). The dimensions of the
    matrix are as follows, in order:

    * magnitude
    * distance
    * longitude
    * latitude
    * epsilon
    * tectonic region type

    Bin edges are defined for all of these dimensions (except tectonic region
    type) as:

    * `mag_bin_edges`
    * `dist_bin_edges`
    * `lat_bin_edges`
    * `lon_bin_edges`
    * `eps_bin_edges`

    The size of the tectonic region type (TRT) dimension is simply determined
    by the length of `trts`.

    Additional metadata for the disaggregation histogram is stored, including
    location (POINT geometry), disaggregation PoE (Probability of Exceedance)
    and the corresponding IML (Intensity Measure Level) extracted from the
    hazard curve, logic tree path information, and investigation time.
    """

    output = djm.OneToOneField('Output', related_name="disagg_matrix")
    lt_realization = djm.ForeignKey('LtRealization')
    investigation_time = djm.FloatField()
    imt = djm.TextField(choices=IMT_CHOICES)
    iml = djm.FloatField()
    poe = djm.FloatField()
    sa_period = djm.FloatField(null=True)
    sa_damping = djm.FloatField(null=True)
    mag_bin_edges = fields.FloatArrayField()
    dist_bin_edges = fields.FloatArrayField()
    lon_bin_edges = fields.FloatArrayField()
    lat_bin_edges = fields.FloatArrayField()
    eps_bin_edges = fields.FloatArrayField()
    trts = fields.CharArrayField()
    location = djm.PointField(srid=DEFAULT_SRID)
    matrix = fields.PickleField()

    class Meta:
        db_table = 'hzrdr\".\"disagg_result'


class UHS(djm.Model):
    """
    UHS/Uniform Hazard Spectra:
    * "Uniform" meaning "the same PoE"
    * "Spectrum" because it covers a range/band of periods/frequencies

    Records in this table contain metadata for a collection of UHS data.
    """
    output = djm.OneToOneField('Output', null=True, related_name="uh_spectra")
    # FK only required for non-statistical results (i.e., mean or quantile
    # curves).
    lt_realization = djm.ForeignKey('LtRealization', null=True)
    investigation_time = djm.FloatField()
    poe = djm.FloatField()
    periods = fields.FloatArrayField()
    STAT_CHOICES = (
        (u'mean', u'Mean'),
        (u'quantile', u'Quantile'),
    )
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)

    class Meta:
        db_table = 'hzrdr\".\"uhs'

    def __iter__(self):
        """
        Iterate over the :class:`UHSData` which belong this object.
        """
        return self.uhsdata_set.iterator()


class UHSData(djm.Model):
    """
    UHS curve for a given location.
    """
    uhs = djm.ForeignKey('UHS')
    imls = fields.FloatArrayField()
    location = djm.PointField(srid=DEFAULT_SRID)

    class Meta:
        db_table = 'hzrdr\".\"uhs_data'


class LtSourceModel(djm.Model):
    """
    Identify a logic tree source model.
    """
    hazard_calculation = djm.ForeignKey('OqJob')
    ordinal = djm.IntegerField()
    sm_lt_path = fields.CharArrayField()
    sm_name = djm.TextField(null=False)
    weight = djm.DecimalField(decimal_places=100, max_digits=101, null=True)

    class Meta:
        db_table = 'hzrdr\".\"lt_source_model'
        ordering = ['ordinal']

    def get_num_sources(self):
        """
        Return the number of sources in the model.
        """
        return sum(info.num_sources for info in self.trtmodel_set.all())

    def get_tectonic_region_types(self):
        """
        Return the tectonic region types in the model,
        ordered by number of sources.
        """
        return self.trtmodel_set.filter(
            lt_model=self, num_ruptures__gt=0).values_list(
            'tectonic_region_type', flat=True)

    def make_gsim_lt(self, trts=()):
        """
        Helper to instantiate a GsimLogicTree object from the logic tree file.
        """
        hc = HazardCalculation(self.hazard_calculation)
        trts = trts or self.get_tectonic_region_types()
        fname = os.path.join(hc.base_path, hc.inputs['gsim_logic_tree'])
        gsim_lt = logictree.GsimLogicTree(
            fname, 'applyToTectonicRegionType', trts)
        for trt in trts:
            if not trt in gsim_lt.values:
                raise ValueError(
                    "Found in %r a tectonic region type %r inconsistent with "
                    "the ones in %r" % (self.sm_name, trt, fname))
        return gsim_lt

    def __iter__(self):
        """
        Yield the realizations corresponding to the given model
        """
        return iter(self.ltrealization_set.all())


class TrtModel(djm.Model):
    """
    Source submodel containing sources of the same tectonic region type.
    """
    lt_model = djm.ForeignKey('LtSourceModel')
    tectonic_region_type = djm.TextField(null=False)
    num_sources = djm.IntegerField(null=False)
    num_ruptures = djm.IntegerField(null=False)
    min_mag = djm.FloatField(null=False)
    max_mag = djm.FloatField(null=False)
    gsims = fields.CharArrayField(null=True)

    def get_realizations(self, gsim_name):
        """
        Return the realizations associated to the current TrtModel and
        the given GSIM.

        :param str gsim_name: name of a GSIM class
        """
        assert gsim_name in self.gsims, gsim_name
        for art in AssocLtRlzTrtModel.objects.filter(
                trt_model=self.id, gsim=gsim_name):
            yield art.rlz

    def get_rlzs_by_gsim(self):
        """
        Return the realizations associated to the current TrtModel
        as an ordered dictionary {gsim_name: [rlz, ...]}
        """
        dic = collections.defaultdict(list)
        for art in AssocLtRlzTrtModel.objects.filter(trt_model=self.id):
            dic[art.gsim].append(art.rlz)
        return collections.OrderedDict(
            (gsim, dic[gsim]) for gsim in sorted(dic))

    def get_gsim_instances(self):
        """
        Return the GSIM instances associated to the current TrtModel
        """
        return [logictree.GSIM[gsim]() for gsim in self.gsims]

    class Meta:
        db_table = 'hzrdr\".\"trt_model'
        ordering = ['id']
        # NB: the TrtModels are built in the right order, see
        # BaseHazardCalculator.initialize_sources


class SourceInfo(djm.Model):
    """
    Source specific infos
    """
    trt_model = djm.ForeignKey('TrtModel')
    source_id = djm.TextField(null=False)
    source_class = djm.TextField(null=False)
    num_sites = djm.IntegerField(null=False)
    num_ruptures = djm.IntegerField(null=False)
    occ_ruptures = djm.IntegerField(null=True)
    uniq_ruptures = djm.IntegerField(null=True)
    calc_time = djm.FloatField(null=False)

    class Meta:
        db_table = 'hzrdr\".\"source_info'
        ordering = ['trt_model', 'source_id']


class AssocLtRlzTrtModel(djm.Model):
    """
    Associations between logic tree realizations and TrtModels. Fixed
    a realization and a TRT, the gsim is unique.
    """
    rlz = djm.ForeignKey('LtRealization')
    trt_model = djm.ForeignKey('TrtModel')
    gsim = djm.TextField(null=False)

    class Meta:
        db_table = 'hzrdr\".\"assoc_lt_rlz_trt_model'
        ordering = ['id']


class LtRealization(djm.Model):
    """
    Identify a logic tree branch.
    """

    lt_model = djm.ForeignKey('LtSourceModel')
    ordinal = djm.IntegerField()
    weight = djm.DecimalField(decimal_places=100, max_digits=101)
    gsim_lt_path = fields.CharArrayField()

    @property
    def sm_lt_path(self):
        """
        The source model logic tree path extracted from the underlying
        source model
        """
        return self.lt_model.sm_lt_path

    class Meta:
        db_table = 'hzrdr\".\"lt_realization'
        ordering = ['ordinal']

    def get_gsim_instances(self):
        """
        Return the GSIM instances associated to the current realization
        by looking at the association table.
        """
        return [logictree.GSIM[art.gsim]() for art in
                AssocLtRlzTrtModel.objects.filter(rlz=self)]


## Tables in the 'riskr' schema.

class LossFraction(djm.Model):
    """
    Holds metadata for loss fraction data
    """
    output = djm.OneToOneField("Output", related_name="loss_fraction")
    variable = djm.TextField(choices=(("taxonomy", "taxonomy"),
                                      ("magnitude_distance",
                                       "Magnitude Distance"),
                                      ("coordinate", "Coordinate")))
    hazard_output = djm.ForeignKey(
        "Output", related_name="risk_loss_fractions")
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)
    poe = djm.FloatField(null=True)
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"loss_fraction'

    def __iter__(self):
        return iter(self.lossfractiondata_set.all())

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.output.hazard_metadata,
                self.statistics, self.quantile,
                self.variable, self.poe, self.loss_type)

    def display_value(self, value, rc):
        """
        Converts `value` in a form that is best suited to be
        displayed.

        :param rc:
           A `RiskCalculation` object used to get the bin width

        :returns: `value` if the attribute `variable` is equal to
           taxonomy. if the attribute `variable` is equal to
           `magnitude-distance`, then it extracts two integers (comma
           separated) from `value` and convert them into ranges
           encoded back as csv.
        """

        if self.variable == "taxonomy":
            return value
        elif self.variable == "magnitude_distance":
            magnitude, distance = map(float, value.split(","))
            return "%.4f,%.4f|%.4f,%.4f" % (
                magnitude * rc.mag_bin_width,
                (magnitude + 1) * rc.mag_bin_width,
                distance * rc.distance_bin_width,
                (distance + 1) * rc.distance_bin_width)
        elif self.variable == "coordinate":
            lon, lat = map(float, value.split(","))
            return "%.4f,%.4f|%.4f,%.4f" % (
                lon * rc.coordinate_bin_width,
                (lon + 1) * rc.coordinate_bin_width,
                lat * rc.coordinate_bin_width,
                (lat + 1) * rc.coordinate_bin_width)
        else:
            raise RuntimeError(
                "disaggregation of type %s not supported" % self.variable)

    def total_fractions(self):
        """
        :returns: a dictionary mapping values of `variable` (e.g. a
        taxonomy) to tuples yielding the associated absolute losses
        (e.g. the absolute losses for assets of a taxonomy) and the
        percentage (expressed in decimal format) over the total losses
        """
        cursor = connections['job_init'].cursor()

        total = self.lossfractiondata_set.aggregate(
            djm.Sum('absolute_loss')).values()[0]

        if not total:
            return {}

        query = """
        SELECT value, sum(absolute_loss)
        FROM riskr.loss_fraction_data
        WHERE loss_fraction_id = %s
        GROUP BY value
        """
        cursor.execute(query, (self.id,))

        rc = self.output.oq_job.risk_calculation

        loss_fraction = collections.namedtuple('loss_fraction', 'bin loss')

        return collections.OrderedDict(
            sorted(
                [loss_fraction(
                    self.display_value(value, rc),
                    (loss, loss / total))
                 for value, loss in cursor],
                key=operator.attrgetter('loss'),
                reverse=True))

    def iteritems(self):
        """
        Yields tuples with two elements. The first one is a location
        (described by a lon/lat tuple), the second one is a dictionary
        modeling the disaggregation of the losses on such location. In
        this dictionary, each key is a value of `variable`, and each
        corresponding value is a tuple holding the absolute losses and
        the fraction of losses occurring in that location.
        """
        rc = self.output.oq_job.risk_calculation
        cursor = connections['job_init'].cursor()

        # Partition by lon,lat because partitioning on geometry types
        # seems not supported in postgis 1.5
        query = """
        SELECT lon, lat, value,
               fraction_loss,
               SUM(fraction_loss) OVER w,
               COUNT(*) OVER w
        FROM (SELECT ST_X(location) as lon,
                     ST_Y(location) as lat,
              value, sum(absolute_loss) as fraction_loss
              FROM riskr.loss_fraction_data
              WHERE loss_fraction_id = %s
              GROUP BY location, value) g
        WINDOW w AS (PARTITION BY lon, lat)
        """

        cursor.execute(query, (self.id, ))

        def display_value_and_fractions(value, absolute_loss, total_loss):
            display_value = self.display_value(value, rc)

            if total_loss > 0:
                fraction = absolute_loss / total_loss
            else:
                # When a rupture is associated with a positive ground
                # shaking (gmv > 0) but with a loss = 0, we still
                # store this information. In that case, total_loss =
                # absolute_loss = 0
                fraction = 0
            return display_value, fraction

        # We iterate on loss fraction data by location in two steps.
        # First we fetch a loss fraction for a single location and a
        # single value. In the same query we get the number `count` of
        # bins stored for such location. Then, we fetch `count` - 1
        # fractions to finalize the fractions on the current location.

        while 1:
            data = cursor.fetchone()
            if data is None:
                raise StopIteration
            lon, lat, value, absolute_loss, total_loss, count = data

            display_value, fraction = display_value_and_fractions(
                value, absolute_loss, total_loss)
            node = [(lon, lat),
                    {display_value: (absolute_loss, fraction)}]

            data = cursor.fetchmany(count - 1)

            for lon, lat, value, absolute_loss, total_loss, count in data:
                display_value, fraction = display_value_and_fractions(
                    value, absolute_loss, total_loss)
                node[1][display_value] = (absolute_loss, fraction)

            node[1] = collections.OrderedDict(
                sorted([(k, v) for k, v in node[1].items()],
                       key=lambda kv: kv[0]))
            yield node

    def to_array(self):
        """
        :returns: the loss fractions as numpy array

        :NOTE:  (not memory efficient)
        """
        def to_tuple():
            for (lon, lat), data in self.iteritems():
                for taxonomy, (absolute_loss, fraction) in data.items():
                    yield lon, lat, taxonomy, absolute_loss, fraction

        return numpy.array(list(to_tuple()), dtype='f4, f4, S3, f4, f4')


class LossFractionData(djm.Model):
    loss_fraction = djm.ForeignKey(LossFraction)
    location = djm.PointField(srid=DEFAULT_SRID)
    value = djm.TextField()
    absolute_loss = djm.TextField()

    class Meta:
        db_table = 'riskr\".\"loss_fraction_data'

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return (self.loss_fraction.output_hash +
                ("%.5f" % self.location.x, "%.5f" % self.location.y,
                 self.value))

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('absolute_loss'))


class LossMap(djm.Model):
    '''
    Holds metadata for loss maps
    '''

    output = djm.OneToOneField("Output", related_name="loss_map")
    hazard_output = djm.ForeignKey("Output", related_name="risk_loss_maps")
    insured = djm.BooleanField(default=False)
    poe = djm.FloatField(null=True)
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"loss_map'

    def __iter__(self):
        return iter(self.lossmapdata_set.all())

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.output.hazard_metadata,
                self.statistics, self.quantile,
                self.insured, self.poe, self.loss_type)


class LossMapData(djm.Model):
    '''
    Holds an asset, its position and a value plus (for
    non-scenario maps) the standard deviation for its loss
    '''

    loss_map = djm.ForeignKey("LossMap")
    asset_ref = djm.TextField()
    value = djm.FloatField()
    std_dev = djm.FloatField(default=0.0, null=True)
    location = djm.PointField(srid=DEFAULT_SRID)

    class Meta:
        db_table = 'riskr\".\"loss_map_data'

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.loss_map.output_hash + (self.asset_ref,)

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('value', 'stddev'))


class AggregateLoss(djm.Model):
    output = djm.OneToOneField("Output", related_name="aggregate_loss")
    insured = djm.BooleanField(default=False)
    mean = djm.FloatField()
    std_dev = djm.FloatField()
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"aggregate_loss'

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.output.hazard_metadata,
                self.insured,
                self.mean, self.std_dev,
                self.loss_type)

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.output_hash

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, lambda x: operator.attrgetter('mean', 'std_dev'))

    def to_csv_str(self):
        """
        Convert AggregateLoss into a CSV string
        """
        return '\n'.join(data.to_csv_str('row-%d' % i)
                         for i, data in enumerate(self, 1))


class LossCurve(djm.Model):
    '''
    Holds the parameters common to a set of loss curves
    '''

    output = djm.OneToOneField("Output", related_name="loss_curve")
    hazard_output = djm.ForeignKey("Output", related_name="risk_loss_curves")
    aggregate = djm.BooleanField(default=False)
    insured = djm.BooleanField(default=False)

    # If the curve is a result of an aggregation over different
    # hazard_output the following fields must be set
    statistics = djm.TextField(null=True, choices=STAT_CHOICES)
    quantile = djm.FloatField(null=True)
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"loss_curve'

    def __iter__(self):
        if self.aggregate:
            return iter([self.aggregatelosscurvedata])
        else:
            return iter(self.losscurvedata_set.all())

    def to_csv_str(self):
        """
        Convert LossCurve into a CSV string
        """
        return '\n'.join(data.to_csv_str('row-%d' % i)
                         for i, data in enumerate(self, 1))

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.output.hazard_metadata,
                self.statistics, self.quantile,
                self.aggregate, self.insured, self.loss_type)


class LossCurveData(djm.Model):
    '''
    Holds the probabilities of exceedance for a given loss curve
    '''

    loss_curve = djm.ForeignKey("LossCurve")
    asset_ref = djm.TextField()
    asset_value = djm.FloatField()
    loss_ratios = fields.FloatArrayField()
    poes = fields.FloatArrayField()
    location = djm.PointField(srid=DEFAULT_SRID)
    average_loss_ratio = djm.FloatField()
    stddev_loss_ratio = djm.FloatField(blank=True, null=True)

    class Meta:
        db_table = 'riskr\".\"loss_curve_data'

    @property
    def losses(self):
        return numpy.array(self.loss_ratios) * self.asset_value

    @property
    def average_loss(self):
        return self.average_loss_ratio * self.asset_value

    @property
    def stddev_loss(self):
        if self.stddev_loss_ratio is not None:
            return self.stddev_loss_ratio * self.asset_value

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.loss_curve.output_hash + (self.asset_ref,)

    def assertAlmostEqual(self, data):
        return loss_curve_almost_equal(self, data)

    def to_csv_str(self, label):
        """
        Convert LossCurveData into a CSV string.

        :param str label:
            an identifier for the curve (for instance the asset_ref)
        """
        ratios = [label, 'Ratios'] + map(str, self.loss_ratios)
        data = ','.join(ratios) + '\n'
        data += ','.join(map(str, [self.asset_value, 'PoE'] + list(self.poes)))
        return data


class AggregateLossCurveData(djm.Model):
    '''
    Holds the probabilities of exceedance for the whole exposure model
    '''

    loss_curve = djm.OneToOneField("LossCurve")
    losses = fields.FloatArrayField()
    poes = fields.FloatArrayField()
    average_loss = djm.FloatField()
    stddev_loss = djm.FloatField(blank=True, null=True)

    class Meta:
        db_table = 'riskr\".\"aggregate_loss_curve_data'

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.loss_curve.output_hash

    def assertAlmostEqual(self, data):
        return loss_curve_almost_equal(self, data)

    def to_csv_str(self, label):
        """
        Convert AggregateLossCurveData into a CSV string.

        :param str label:
            an identifier for the curve (for instance the cost type)
        """
        data = ','.join(map(str, [label, 'Losses'] + list(self.losses))) + '\n'
        data += ','.join(map(str, ['', 'PoE'] + list(self.poes)))
        return data


class EventLoss(djm.Model):
    """
    Holds the aggregate loss we have for each rupture
    """

    #: Foreign key to an :class:`openquake.engine.db.models.Output`
    #: object with output_type == event_loss
    output = djm.OneToOneField('Output', related_name="event_loss")
    hazard_output = djm.ForeignKey(
        "Output", related_name="risk_event_loss_tables")
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"event_loss'

    def __iter__(self):
        return iter(self.eventlossdata_set.all().order_by('-aggregate_loss'))

    def to_csv_str(self):
        """
        Convert EventLoss into a CSV with fields rupture_tag, aggregate_loss
        """
        return '\n'.join('%s,%s' % (self.rupture.tag, self.aggregate_loss)
                         for data in self)

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        # FIXME(lp) this is not db-sequence independent
        return (self.output.output_type, self.output.hazard_metadata,
                self.loss_type)


class EventLossData(djm.Model):
    event_loss = djm.ForeignKey(EventLoss)
    rupture = djm.ForeignKey('SESRupture')
    aggregate_loss = djm.FloatField()

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.event_loss.output_hash + (self.rupture_id,)

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('aggregate_loss'))

    class Meta:
        db_table = 'riskr\".\"event_loss_data'

    def to_csv_str(self):
        """
        Convert EventLossData into a CSV string
        """
        return '%s,%s,%s' % (self.rupture.tag, self.rupture.rupture.mag,
                             self.aggregate_loss)


class BCRDistribution(djm.Model):
    '''
    Holds metadata for the benefit-cost ratio distribution
    '''

    output = djm.OneToOneField("Output", related_name="bcr_distribution")
    hazard_output = djm.ForeignKey(
        "Output", related_name="risk_bcr_distribution")
    loss_type = djm.TextField(choices=zip(LOSS_TYPES, LOSS_TYPES))

    class Meta:
        db_table = 'riskr\".\"bcr_distribution'

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.output.hazard_metadata,
                self.loss_type)


class BCRDistributionData(djm.Model):
    '''
    Holds the actual data for the benefit-cost ratio distribution
    '''

    bcr_distribution = djm.ForeignKey("BCRDistribution")
    asset_ref = djm.TextField()
    average_annual_loss_original = djm.FloatField()
    average_annual_loss_retrofitted = djm.FloatField()
    bcr = djm.FloatField()
    location = djm.PointField(srid=DEFAULT_SRID)

    class Meta:
        db_table = 'riskr\".\"bcr_distribution_data'

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.bcr_distribution.output_hash + (self.asset_ref,)

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data,
            operator.attrgetter('average_annual_loss_original',
                                'average_loss_retrofitted',
                                'bcr'))


class DmgState(djm.Model):
    """Holds the damage_states associated to a given output"""
    # they actually come from the fragility model xml input
    risk_calculation = djm.ForeignKey("RiskCalculation")
    dmg_state = djm.TextField(
        help_text="The name of the damage state")
    lsi = djm.PositiveSmallIntegerField(
        help_text="limit state index, to order the limit states")

    class Meta:
        db_table = 'riskr\".\"dmg_state'


class DmgDistPerAsset(djm.Model):
    """Holds the actual data for damage distributions per asset."""

    dmg_state = djm.ForeignKey("DmgState")
    exposure_data = djm.ForeignKey("ExposureData")
    mean = djm.FloatField()
    stddev = djm.FloatField()

    class Meta:
        db_table = 'riskr\".\"dmg_dist_per_asset'

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.dmg_state.dmg_state, self.exposure_data.asset_ref)

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.output_hash

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('mean', 'stddev'))

    @property
    def output(self):
        return self.dmg_state.rc_calculation.oqjob.output_set.get(
            output_type="dmg_dist_per_asset")


class DmgDistPerTaxonomy(djm.Model):
    """Holds the actual data for damage distributions per taxonomy."""

    dmg_state = djm.ForeignKey("DmgState")
    taxonomy = djm.TextField()
    mean = djm.FloatField()
    stddev = djm.FloatField()

    class Meta:
        db_table = 'riskr\".\"dmg_dist_per_taxonomy'

    @property
    def output(self):
        return self.dmg_state.rc_calculation.oqjob.output_set.get(
            output_type="dmg_dist_per_taxonomy")

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type,
                self.dmg_state.dmg_state, self.taxonomy)

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.output_hash

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('mean', 'stddev'))


class DmgDistTotal(djm.Model):
    """Holds the actual 'total damage distribution' values for for an entire
    calculation. There should be  one record per calculation per damage state.
    """

    dmg_state = djm.ForeignKey("DmgState")
    mean = djm.FloatField()
    stddev = djm.FloatField()

    class Meta:
        db_table = 'riskr\".\"dmg_dist_total'

    @property
    def output(self):
        return self.dmg_state.rc_calculation.oqjob.output_set.get(
            output_type="dmg_dist_total")

    @property
    def output_hash(self):
        """
        :returns:
            a (db-sequence independent) tuple that identifies this output among
            which the ones created in the same calculation
        """
        return (self.output.output_type, self.dmg_state.dmg_state, )

    @property
    def data_hash(self):
        """
        A db-sequence independent tuple that identifies this output
        """
        return self.output_hash

    def assertAlmostEqual(self, data):
        return risk_almost_equal(
            self, data, operator.attrgetter('mean', 'stddev'))


## Tables in the 'riski' schema.


class ExposureModel(djm.Model):
    '''
    A risk exposure model
    '''

    job = djm.OneToOneField("OqJob")
    name = djm.TextField()
    description = djm.TextField(null=True)
    category = djm.TextField()
    taxonomy_source = djm.TextField(
        null=True, help_text="the taxonomy system used to classify the assets")
    AREA_CHOICES = (
        (u'aggregated', u'Aggregated area value'),
        (u'per_asset', u'Per asset area value'),
    )
    area_type = djm.TextField(null=True, choices=AREA_CHOICES)
    area_unit = djm.TextField(null=True)
    deductible_absolute = djm.BooleanField(default=True)
    insurance_limit_absolute = djm.BooleanField(default=True)

    class Meta:
        db_table = 'riski\".\"exposure_model'

    def taxonomies_in(self, region_constraint):
        """
        :param str region_constraint:
            polygon in wkt format the assets must be contained into
        :returns:
            A dictionary mapping each taxonomy with the number of assets
            contained in `region_constraint`
        """

        return ExposureData.objects.taxonomies_contained_in(
            self.id, region_constraint)

    def unit(self, loss_type):
        if loss_type == "fatalities":
            return "people"
        else:
            return self.costtype_set.get(name=loss_type).unit

    def has_insurance_bounds(self):
        return not self.exposuredata_set.filter(
            (djm.Q(cost__deductible_absolute__isnull=True) |
             djm.Q(cost__insurance_limit_absolute__isnull=True))).exists()

    def has_retrofitted_costs(self):
        return not (
            self.exposuredata_set.filter(
                cost__converted_retrofitted_cost__isnull=True)).exists()

    def has_time_event(self, time_event):
        return (
            self.exposuredata_set.filter(occupancy__period=time_event).count()
            ==
            self.exposuredata_set.count())

    def supports_loss_type(self, loss_type):
        """
        :returns:
            True if the exposure contains the asset data needed
            for computing losses for `loss_type`
        """
        if loss_type != "fatalities":
            ct = loss_type_to_cost_type(loss_type)
            return self.exposuredata_set.filter(
                cost__cost_type__name=ct).exists()
        else:
            if self.category == "population":
                return not self.exposuredata_set.filter(
                    number_of_units__isnull=True).exists()
            else:
                return not self.exposuredata_set.filter(
                    occupancy__isnull=True).exists()


class CostType(djm.Model):
    COST_TYPE_CHOICES = (
        ("structuralCost", "structuralCost"),
        ("retrofittedStructuralCost", "retrofittedStructuralCost"),
        ("nonStructuralCost", "nonStructuralCost"),
        ("contentsCost", "contentsCost"),
        ("businessInterruptionCost", "businessInterruptionCost"))
    CONVERSION_CHOICES = (
        (u'aggregated', u'Aggregated economic value'),
        (u'per_area', u'Per area economic value'),
        (u'per_asset', u'Per asset economic value'),
    )

    exposure_model = djm.ForeignKey(ExposureModel)
    name = djm.TextField(choices=COST_TYPE_CHOICES)
    conversion = djm.TextField(choices=CONVERSION_CHOICES)
    unit = djm.TextField(null=True)
    retrofitted_conversion = djm.TextField(
        null=True, choices=CONVERSION_CHOICES)
    retrofitted_unit = djm.TextField(null=True)

    class Meta:
        db_table = 'riski\".\"cost_type'


class Occupancy(djm.Model):
    '''
    Asset occupancy data
    '''

    exposure_data = djm.ForeignKey("ExposureData")
    period = djm.TextField()
    occupants = djm.FloatField()

    class Meta:
        db_table = 'riski\".\"occupancy'


class AssetManager(djm.GeoManager):
    """
    Asset manager
    """

    def get_asset_chunk(self, rc, taxonomy, offset=0, size=None,
                        asset_ids=None):
        """
        :returns:

           a list of instances of
           :class:`openquake.engine.db.models.ExposureData` (ordered
           by location) contained in `region_constraint`(embedded in
           the risk calculation `rc`) of `taxonomy` associated with
           the `openquake.engine.db.models.ExposureModel` associated
           with `rc`.

           It also add an annotation to each ExposureData object to provide the
           occupants value for the risk calculation given in input and the cost
           for each cost type considered in `rc`
        """

        query, args = self._get_asset_chunk_query_args(
            rc, taxonomy, offset, size, asset_ids)
        return list(self.raw(query, args))

    def _get_asset_chunk_query_args(
            self, rc, taxonomy, offset, size, asset_ids):
        """
        Build a parametric query string and the corresponding args for
        #get_asset_chunk
        """
        args = (rc.exposure_model.id, taxonomy,
                "SRID=4326; %s" % rc.region_constraint.wkt)

        people_field, occupants_cond, occupancy_join, occupants_args = (
            self._get_people_query_helper(
                rc.exposure_model.category, rc.time_event))

        args += occupants_args

        if asset_ids is None:
            assets_cond = 'true'
        else:
            assets_cond = 'riski.exposure_data.id IN (%s)' % ', '.join(
                map(str, asset_ids))
        cost_type_fields, cost_type_joins = self._get_cost_types_query_helper(
            rc.exposure_model.costtype_set.all())

        query = """
            SELECT riski.exposure_data.*,
                   {people_field} AS people,
                   {costs}
            FROM riski.exposure_data
            {occupancy_join}
            ON riski.exposure_data.id = riski.occupancy.exposure_data_id
            {costs_join}
            WHERE exposure_model_id = %s AND
                  taxonomy = %s AND
                  ST_COVERS(ST_GeographyFromText(%s), site) AND
                  {occupants_cond} AND {assets_cond}
            GROUP BY riski.exposure_data.id
            ORDER BY ST_X(geometry(site)), ST_Y(geometry(site))
            """.format(people_field=people_field,
                       occupants_cond=occupants_cond,
                       assets_cond=assets_cond,
                       costs=cost_type_fields,
                       costs_join=cost_type_joins,
                       occupancy_join=occupancy_join)
        if offset:
            query += 'OFFSET %d ' % offset
        if size is not None:
            query += 'LIMIT %d' % size
        return query, args

    def _get_people_query_helper(self, category, time_event):
        """
        Support function for _get_asset_chunk_query_args
        """
        args = ()
        # if the exposure model is of type "population" we extract the
        # data from the `number_of_units` field
        if category == "population":
            occupants_field = "number_of_units"
            occupants_cond = "1 = 1"
            occupancy_join = ""
        else:
            # otherwise we will "left join" the occupancy table
            occupancy_join = "LEFT JOIN riski.occupancy"
            occupants_field = "AVG(riski.occupancy.occupants)"

            # and the time_event is not specified we compute the
            # number of occupants by averaging the occupancy data for
            # each asset, otherwise we get the unique proper occupants
            # value.
            if time_event is None:
                occupants_cond = "1 = 1"
            else:
                args += (time_event,)
                occupants_cond = "riski.occupancy.period = %s"
        return occupants_field, occupants_cond, occupancy_join, args

    def _get_cost_types_query_helper(self, cost_types):
        """
        Support function for _get_asset_chunk_query_args
        """
        # For each cost type associated with the exposure model we
        # join the `cost` table to the current queryset in order to
        # lookup for a cost value for each asset.

        # Actually we extract 4 values: the cost, the retrofitted
        # cost, the deductible and the insurance limit

        costs = []
        costs_join = ""

        for cost_type in cost_types:
            # here the max value is irrelevant as we are sureto join
            # against one row
            costs.append("max(%s.converted_cost) AS %s" % (cost_type.name,
                                                           cost_type.name))
            costs.append(
                "max(%s.converted_retrofitted_cost) AS retrofitted_%s" % (
                    cost_type.name, cost_type.name))
            costs.append(
                "max(%s.deductible_absolute) AS deductible_%s" % (
                    cost_type.name, cost_type.name))
            costs.append(
                "max(%s.insurance_limit_absolute) AS insurance_limit_%s" % (
                    cost_type.name, cost_type.name))

            costs_join += """
            LEFT JOIN riski.cost AS %(name)s
            ON %(name)s.cost_type_id = '%(id)s' AND
            %(name)s.exposure_data_id = riski.exposure_data.id""" % dict(
                name=cost_type.name, id=cost_type.id)

        return ", ".join(costs), costs_join

    def taxonomies_contained_in(self, exposure_model_id, region_constraint):
        """

        :returns:
            A dictionary which map each taxonomy associated with
            `exposure_model` and contained in `region_constraint` with the
            number of assets.
        """
        cursor = connections['job_init'].cursor()

        cursor.execute("""
        SELECT riski.exposure_data.taxonomy, COUNT(*)
        FROM riski.exposure_data WHERE
        exposure_model_id = %s AND ST_COVERS(ST_GeographyFromText(%s), site)
        group by riski.exposure_data.taxonomy
        """, [exposure_model_id, "SRID=4326; %s" % region_constraint.wkt])

        return dict(cursor)


class ExposureData(djm.Model):
    '''
    Per-asset risk exposure data
    '''

    NO_RETROFITTING_COST = "no retrofitting cost"

    exposure_model = djm.ForeignKey("ExposureModel")
    asset_ref = djm.TextField()
    taxonomy = djm.TextField()
    site = djm.PointField(geography=True)

    number_of_units = djm.FloatField(
        null=True, help_text="number of assets, people, etc.")
    area = djm.FloatField(null=True)

    objects = AssetManager()

    class Meta:
        db_table = 'riski\".\"exposure_data'
        ordering = ['asset_ref']

    def __str__(self):
        return "%s (%s-%s @ %s)" % (
            self.id, self.exposure_model_id, self.asset_ref, self.site)

    @staticmethod
    def per_asset_value(
            cost, cost_type, area, area_type, number_of_units, category):
        """
        Return per-asset value for the given exposure data set.

        Calculate per asset value by considering the given exposure
        data as follows:

            case 1: cost type: aggregated:
                cost = economic value
            case 2: cost type: per asset:
                cost * number (of assets) = economic value
            case 3: cost type: per area and area type: aggregated:
                cost * area = economic value
            case 4: cost type: per area and area type: per asset:
                cost * area * number = economic value

        The same "formula" applies to contenst/retrofitting cost analogously.

        :returns:
            The per-asset value as a `float`.
        :raises:
            `ValueError` in case of a malformed (risk exposure data) input.
        """
        if category is not None and category == "population":
            return number_of_units
        if cost_type == "aggregated":
            return cost
        elif cost_type == "per_asset":
            return cost * number_of_units
        elif cost_type == "per_area":
            if area_type == "aggregated":
                return cost * area
            elif area_type == "per_asset":
                return cost * area * number_of_units
        raise ValueError("Invalid input")

    # we expect several annotations depending on "loss_type" to be
    # present. See `get_asset_chunk` for details.

    def value(self, loss_type):
        """
        Extract the value of the asset for the given `loss_type`.
        Although the Django Model definition does not have a value for
        each loss type, we rely on the fact that an annotation on the
        asset named `loss_type` is present.
        """
        if loss_type == "fatalities":
            return getattr(self, "people")
        return getattr(self, loss_type)

    def retrofitted(self, loss_type):
        """
        Extract the retrofitted cost of the asset for the given
        `loss_type`. See the method `value` for details.
        """
        return getattr(self, "retrofitted_%s" % loss_type)

    def deductible(self, loss_type):
        """
        Extract the deductible limit of the asset for the given
        `loss_type`. See the method `value` for details.
        """
        return (getattr(self, "deductible_%s" % loss_type) /
                getattr(self, loss_type))

    def insurance_limit(self, loss_type):
        """
        Extract the insurance limit of the asset for the given
        `loss_type`. See the method `value` for details.
        """
        return (getattr(self, "insurance_limit_%s" % loss_type) /
                getattr(self, loss_type))


def make_absolute(limit, value, is_absolute=None):
    """
    :returns:
        `limit` if `is_absolute` is True or `limit` is None,
        else `limit` * `value`
    """
    if limit is not None:
        if not is_absolute:
            return value * limit
        else:
            return limit


class Cost(djm.Model):
    exposure_data = djm.ForeignKey(ExposureData)
    cost_type = djm.ForeignKey(CostType)
    converted_cost = djm.FloatField()
    converted_retrofitted_cost = djm.FloatField(null=True)
    deductible_absolute = djm.FloatField(null=True, blank=True)
    insurance_limit_absolute = djm.FloatField(null=True, blank=True)

    class Meta:
        db_table = 'riski\".\"cost'

## Tables in the 'htemp' schema.


class HazardSite(djm.Model):
    """
    Contains pre-computed site parameter matrices. ``lons`` and ``lats``
    represent the calculation sites of interest. The associated site parameters
    are from the closest point in a site model in relation to each calculation
    point of interest.

    Used only if a calculation defines a site model (otherwise, reference
    parameters are use for all points of interest).
    """

    hazard_calculation = djm.ForeignKey('OqJob')
    location = djm.PointField(srid=DEFAULT_SRID)

    class Meta:
        db_table = 'hzrdi\".\"hazard_site'


class AssetSite(djm.Model):
    """
    Contains the association exposure_data_id -> site_id, as generated
    by the current risk job.
    """
    job = djm.ForeignKey('OqJob', null=False)
    asset = djm.ForeignKey('ExposureData', null=False)
    site = djm.ForeignKey('HazardSite', null=False)

    class Meta:
        db_table = 'riskr\".\"asset_site'
        ordering = ['asset']

    def __repr__(self):
        return '<%s=%d, (%d, %d)>' % (self.__class__.__name__,
                                      self.id, self.asset.id, self.site.id)


class Epsilon(djm.Model):
    """
    Contains the association (asset_site, ses_collection) -> epsilons,
    as generated by the current risk job for event based and scenario
    computations.
    """
    asset_site = djm.ForeignKey('AssetSite', null=False)
    ses_collection = djm.ForeignKey('SESCollection', null=False)
    epsilons = fields.FloatArrayField(null=False)

    @classmethod
    def saveall(cls, ses_coll, asset_sites, epsilon_matrix):
        """
        Insert the epsilon matrix associated to the given
        SES collection for each asset_sites association.

        :param ses_coll:
            a :class:`openquake.engine.db.models.SESCollection` instance
        :param asset_sites:
            a list of :class:`openquake.engine.db.models.AssetSite` instances
        :param epsilon_matrix:
            a numpy matrix with NxE elements, where `N` is the number of assets
            and `E` the number of events for the given SESCollection
        """
        assert len(asset_sites) == len(epsilon_matrix), (
            len(asset_sites), len(epsilon_matrix))
        data = [cls(asset_site=asset_site,
                    ses_collection=ses_coll,
                    epsilons=list(epsilons))
                for asset_site, epsilons in zip(asset_sites, epsilon_matrix)]
        return writer.CacheInserter.saveall(data)

    class Meta:
        db_table = 'riskr\".\"epsilon'
        ordering = ['asset_site']

    def __repr__(self):
        return '<%s %r %r>' % (self.__class__.__name__, self.asset_site,
                               self.ses_collection)
