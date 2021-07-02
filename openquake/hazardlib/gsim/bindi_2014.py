# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2014-2021 GEM Foundation
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake. If not, see <http://www.gnu.org/licenses/>.

"""
Module exports :class:`BindiEtAl2014Rjb`,
               :class:`BindiEtAl2014RjbEC8`,
               :class:`BindiEtAl2014RjbEC8NoSOF`,
               :class:`BindiEtAl2014Rhyp`,
               :class:`BindiEtAl2014RhypEC8`,
               :class:`BindiEtAl2014RhypEC8NoSOF`
"""
import numpy as np
from scipy.constants import g

from openquake.baselib.general import CallableDict
from openquake.hazardlib.gsim.base import GMPE, CoeffsTable
from openquake.hazardlib import const
from openquake.hazardlib.imt import PGA, PGV, SA

CONSTS = {"Mref": 5.5,
          "Mh": 6.75,
          "Rref": 1.0,
          "Vref": 800.0}


def _get_distance_scaling_term(C, rval, mag):
    """
    Returns the distance scaling term of the GMPE described in equation 2
    """
    r_adj = np.sqrt(rval ** 2.0 + C["h"] ** 2.0)
    return (
        (C["c1"] + C["c2"] * (mag - CONSTS["Mref"])) *
        np.log10(r_adj / CONSTS["Rref"]) -
        (C["c3"] * (r_adj - CONSTS["Rref"])))


def _get_magnitude_scaling_term(C, mag):
    """
    Returns the magnitude scaling term of the GMPE described in
    equation 3
    """
    dmag = mag - CONSTS["Mh"]
    if mag < CONSTS["Mh"]:
        return C["e1"] + (C["b1"] * dmag) + (C["b2"] * (dmag ** 2.0))
    else:
        return C["e1"] + (C["b3"] * dmag)


def _get_mean(kind, sof, C, rup, dists, sites):
    """
    Returns the mean ground motion
    """
    sof_term = _get_style_of_faulting_term(C, rup) if sof else 0.
    return (_get_magnitude_scaling_term(C, rup.mag) +
            _get_distance_scaling_term(C, dists, rup.mag) +
            _get_site_amplification_term(kind, C, sites.vs30) + sof_term)


_get_site_amplification_term = CallableDict()


@_get_site_amplification_term.add("base")
def _get_site_amplification_term_1(kind, C, vs30):
    """
    Returns the site amplification term for the case in which Vs30
    is used directly
    """
    return C["gamma"] * np.log10(vs30 / CONSTS["Vref"])


@_get_site_amplification_term.add("EC8")
def _get_site_amplification_term_2(kind, C, vs30):
    """
    Returns the site amplification given Eurocode 8 site classification
    """
    f_s = np.zeros_like(vs30)
    # Site class B
    idx = np.logical_and(vs30 < 800.0, vs30 >= 360.0)
    f_s[idx] = C["eB"]
    # Site Class C
    idx = np.logical_and(vs30 < 360.0, vs30 >= 180.0)
    f_s[idx] = C["eC"]
    # Site Class D
    idx = vs30 < 180.0
    f_s[idx] = C["eD"]
    return f_s


def _get_stddevs(C, stddev_types, num_sites):
    """
    Return standard deviations as defined in table 2.
    """
    stddevs = []
    for stddev_type in stddev_types:
        if stddev_type == const.StdDev.TOTAL:
            stddevs.append(C['sigma'] + np.zeros(num_sites))
        elif stddev_type == const.StdDev.INTRA_EVENT:
            stddevs.append(C['phi'] + np.zeros(num_sites))
        elif stddev_type == const.StdDev.INTER_EVENT:
            stddevs.append(C['tau'] + np.zeros(num_sites))
    return stddevs


def _get_style_of_faulting_term(C, rup):
    """
    Returns the style-of-faulting term.
    Fault type (Strike-slip, Normal, Thrust/reverse) is
    derived from rake angle.
    Rakes angles within 30 of horizontal are strike-slip,
    angles from 30 to 150 are reverse, and angles from
    -30 to -150 are normal.
    Note that the 'Unspecified' case is not considered in this class
    as rake is required as an input variable
    """
    SS, NS, RS = 0.0, 0.0, 0.0
    if np.abs(rup.rake) <= 30.0 or (180.0 - np.abs(rup.rake)) <= 30.0:
        # strike-slip
        SS = 1.0
    elif rup.rake > 30.0 and rup.rake < 150.0:
        # reverse
        RS = 1.0
    else:
        # normal
        NS = 1.0
    return (C["sofN"] * NS) + (C["sofR"] * RS) + (C["sofS"] * SS)


class BindiEtAl2014Rjb(GMPE):
    """
    Implements European GMPE:
    D.Bindi, M. Massa, L.Luzi, G. Ameri, F. Pacor, R.Puglia and P. Augliera
    (2014), "Pan-European ground motion prediction equations for the
    average horizontal component of PGA, PGV and 5 %-damped PSA at spectral
    periods of up to 3.0 s using the RESORCE dataset", Bulletin of
    Earthquake Engineering, 12(1), 391 - 340

    The regressions are developed considering the geometrical mean of the
    as-recorded horizontal components
    The printed version of the GMPE was corrected by Erratum:
    D.Bindi, M. Massa, L.Luzi, G. Ameri, F. Pacor, R.Puglia and P. Augliera
    (2014), "Erratum to Pan-European ground motion prediction equations for the
    average horizontal component of PGA, PGV and 5 %-damped PSA at spectral
    periods of up to 3.0 s using the RESORCE dataset", Bulletin of
    Earthquake Engineering, 12(1), 431 - 448. The erratum notes that the
    printed coefficients tables were in error. In this implementation
    coefficients tables were taken from the Electronic Supplementary
    material of the original paper, which are indicated as being unaffected.
    """
    kind = "base"

    #: Supported tectonic region type is 'active shallow crust'
    DEFINED_FOR_TECTONIC_REGION_TYPE = const.TRT.ACTIVE_SHALLOW_CRUST

    #: Set of :mod:`intensity measure types <openquake.hazardlib.imt>`
    #: this GSIM can calculate. A set should contain classes from module
    #: :mod:`openquake.hazardlib.imt`.
    DEFINED_FOR_INTENSITY_MEASURE_TYPES = {PGA, PGV, SA}

    #: Supported intensity measure component is the geometric mean of two
    #: horizontal components
    DEFINED_FOR_INTENSITY_MEASURE_COMPONENT = const.IMC.AVERAGE_HORIZONTAL

    #: Supported standard deviation types are inter-event, intra-event
    #: and total
    DEFINED_FOR_STANDARD_DEVIATION_TYPES = {
        const.StdDev.TOTAL, const.StdDev.INTER_EVENT, const.StdDev.INTRA_EVENT}

    #: Required site parameter is only Vs30
    REQUIRES_SITES_PARAMETERS = {'vs30'}

    #: Required rupture parameters are magnitude and rake (eq. 1).
    REQUIRES_RUPTURE_PARAMETERS = {'rake', 'mag'}

    #: Required distance measure is Rjb (eq. 1).
    REQUIRES_DISTANCES = {'rjb'}

    sof = True

    def __init__(self, adjustment_factor=1.0, **kwargs):
        super().__init__(adjustment_factor=adjustment_factor, **kwargs)
        self.adjustment_factor = np.log(adjustment_factor)
        [self.dist_type] = self.REQUIRES_DISTANCES

    def get_mean_and_stddevs(self, sites, rup, dists, imt, stddev_types):
        """
        See :meth:`superclass method
        <.base.GroundShakingIntensityModel.get_mean_and_stddevs>`
        for spec of input and result values.
        """
        # extracting dictionary of coefficients specific to required
        # intensity measure type.

        C = self.COEFFS[imt]
        imean = _get_mean(self.kind, self.sof, C, rup,
                          getattr(dists, self.dist_type), sites)
        if imt.name in "SA PGA":
            # Convert units to g,
            # but only for PGA and SA (not PGV):
            mean = np.log((10.0 ** (imean - 2.0)) / g)
        else:
            # PGV:
            mean = np.log(10.0 ** imean)

        istddevs = _get_stddevs(C, stddev_types, len(sites.vs30))
        stddevs = np.log(10.0 ** np.array(istddevs))
        return mean + self.adjustment_factor, stddevs

    #: Coefficients from Table 2

    COEFFS = CoeffsTable(sa_damping=5, table="""
    imt             e1             c1            c2             h            c3             b1             b2            b3          gamma           sofN           sofR           sofS           tau           phi        phis2s         sigma
    pgv    2.264810000   -1.224080000   0.202085000   5.061240000   0.000000000    0.162802000   -0.092632400   0.044030100   -0.529443000   -0.009476750    0.040057400   -0.030580500   0.156062000   0.277714000   0.120398000   0.318560000
    pga    3.328190000   -1.239800000   0.217320000   5.264860000   0.001186240   -0.085504500   -0.092563900   0.000000000   -0.301899000   -0.039769500    0.077525300   -0.037755800   0.149977000   0.282398000   0.165611000   0.319753000
    0.02   3.370530000   -1.263580000   0.220527000   5.200820000   0.001118160   -0.089055400   -0.091615200   0.000000000   -0.294021000   -0.039236000    0.081051600   -0.041815600   0.158670000   0.282356000   0.183959000   0.323885000
    0.04   3.439220000   -1.310250000   0.244676000   4.916690000   0.001091830   -0.116919000   -0.078378900   0.000000000   -0.241765000   -0.037720400    0.079778300   -0.042057900   0.154621000   0.291143000   0.187409000   0.329654000
    0.07   3.596510000   -1.290510000   0.231878000   5.359220000   0.001820940   -0.085012400   -0.056996800   0.000000000   -0.207629000   -0.045943700    0.087496800   -0.041553000   0.172785000   0.291499000   0.199913000   0.338860000
    0.10   3.686380000   -1.281780000   0.219406000   6.121460000   0.002114430   -0.113550000   -0.075332500   0.000000000   -0.173237000   -0.038052800    0.084710300   -0.046658500   0.169691000   0.301967000   0.208178000   0.346379000
    0.15   3.686320000   -1.176970000   0.182662000   5.741540000   0.002540270   -0.092872600   -0.102433000   0.073904200   -0.202492000   -0.026729300    0.067844100   -0.041114700   0.152902000   0.305804000   0.212124000   0.341900000
    0.20   3.682620000   -1.103010000   0.133154000   5.319980000   0.002420890    0.010085700   -0.105184000   0.150461000   -0.291228000   -0.032653700    0.075976900   -0.043323200   0.150055000   0.300109000   0.190469000   0.335532000
    0.26   3.643140000   -1.085270000   0.115603000   5.134550000   0.001964370    0.029939700   -0.127173000   0.178899000   -0.354425000   -0.033843800    0.074982000   -0.041138100   0.151209000   0.302419000   0.187037000   0.338114000
    0.30   3.639850000   -1.105910000   0.108276000   5.128460000   0.001499220    0.039190400   -0.138578000   0.189682000   -0.393060000   -0.037245300    0.076701100   -0.039455900   0.157946000   0.297402000   0.174118000   0.336741000
    0.36   3.574800000   -1.099550000   0.103083000   4.905570000   0.001049050    0.052103000   -0.151385000   0.216011000   -0.453905000   -0.027906700    0.069789800   -0.041883200   0.165436000   0.294395000   0.175848000   0.337694000
    0.40   3.530060000   -1.095380000   0.101111000   4.953860000   0.000851474    0.045846400   -0.162090000   0.224827000   -0.492063000   -0.025630900    0.072566800   -0.046936000   0.157728000   0.296992000   0.169883000   0.336278000
    0.46   3.433870000   -1.065860000   0.109066000   4.659900000   0.000868165    0.060083800   -0.165897000   0.197716000   -0.564463000   -0.018663500    0.064599300   -0.045935800   0.173005000   0.291868000   0.164162000   0.339290000
    0.50   3.405540000   -1.057670000   0.112197000   4.432050000   0.000788528    0.088318900   -0.164108000   0.154750000   -0.596196000   -0.017419400    0.060282600   -0.042863200   0.180820000   0.289957000   0.165090000   0.341717000
    0.60   3.304420000   -1.050140000   0.121734000   4.216570000   0.000487285    0.120182000   -0.163325000   0.117576000   -0.667824000   -0.000486417    0.044920900   -0.044434500   0.182233000   0.292223000   0.175634000   0.344388000
    0.70   3.238820000   -1.050210000   0.114674000   4.171270000   0.000159408    0.166933000   -0.161112000   0.112005000   -0.738390000    0.011203300    0.028150600   -0.039353900   0.189396000   0.289307000   0.168617000   0.345788000
    0.80   3.153700000   -1.046540000   0.129522000   4.200160000   0.000000000    0.193817000   -0.156553000   0.051728500   -0.794076000    0.016525800    0.020352200   -0.036878300   0.189074000   0.288815000   0.168170000   0.345200000
    0.90   3.134810000   -1.046120000   0.114536000   4.480030000   0.000000000    0.247547000   -0.153819000   0.081575400   -0.821699000    0.016449300    0.021242200   -0.037691300   0.191986000   0.293264000   0.183719000   0.350517000
    1.00   3.124740000   -1.052700000   0.103471000   4.416130000   0.000000000    0.306569000   -0.147558000   0.092837300   -0.826584000    0.026307100    0.018604300   -0.044911100   0.195026000   0.297907000   0.200775000   0.356067000
    1.30   2.898410000   -0.973828000   0.104898000   4.258210000   0.000000000    0.349119000   -0.149483000   0.108209000   -0.845047000    0.025233900    0.022362100   -0.047595700   0.181782000   0.306676000   0.209625000   0.356504000
    1.50   2.847270000   -0.983388000   0.109072000   4.566970000   0.000000000    0.384546000   -0.139867000   0.098737200   -0.823200000    0.018673800    0.023089400   -0.041763000   0.177752000   0.316312000   0.218569000   0.362835000
    1.80   2.680160000   -0.983082000   0.164027000   4.680080000   0.000000000    0.343663000   -0.135933000   0.000000000   -0.778657000    0.011371300    0.016688200   -0.028059400   0.163242000   0.326484000   0.221367000   0.365020000
    2.00   2.601710000   -0.979215000   0.163344000   4.581860000   0.000000000    0.331747000   -0.148282000   0.000000000   -0.769243000    0.005535450    0.019856600   -0.025392000   0.164958000   0.329916000   0.225350000   0.368857000
    2.60   2.390670000   -0.977532000   0.211831000   5.395170000   0.000000000    0.357514000   -0.122539000   0.000000000   -0.769609000    0.008734600    0.023314200   -0.032048600   0.170280000   0.320626000   0.210193000   0.363037000
    3.00   2.253990000   -0.940373000   0.227241000   5.741730000   0.000000000    0.385526000   -0.111445000   0.000000000   -0.732072000    0.022989300   -0.020662000   -0.002327150   0.176546000   0.314165000   0.207247000   0.360373000
    """)


class BindiEtAl2014RjbEC8(BindiEtAl2014Rjb):
    """
    Implements the Bindi et al (2014) GMPE for the case where Joyner-Boore
    distance is specified but Eurocode 8 Site classification is used.
    """
    kind = "EC8"

    #: Coefficients from Table 1
    COEFFS = CoeffsTable(sa_damping=5, table="""
    imt             e1             c1            c2             h            c3             b1             b2            b3            eA            eB            eC            eD           sofN          sofR           sofS          sofU           tau           phi        phis2s         sigma
    pgv    2.375220000   -1.304700000   0.209460000   5.761910000   0.000000000    0.273952000   -0.051424900   0.000000000   0.000000000   0.122258000   0.276738000   0.380306000   -0.001827210   0.057498900    0.022657800   0.000000000   0.186089000   0.271268000   0.177104000   0.328961000
    pga    3.450780000   -1.360610000   0.215873000   6.147170000   0.000732525   -0.020871500   -0.072242500   0.000000000   0.000000000   0.137715000   0.233048000   0.214227000   -0.032284600   0.073677800   -0.019431300   0.000000000   0.180904000   0.276335000   0.206288000   0.330284000
    0.02   3.478060000   -1.375190000   0.218095000   5.906840000   0.000710063   -0.026825000   -0.072604300   0.000000000   0.000000000   0.134904000   0.226827000   0.213357000   -0.028085300   0.077531800   -0.020641400   0.000000000   0.182533000   0.278823000   0.208393000   0.333258000
    0.04   3.580060000   -1.433270000   0.238839000   5.793940000   0.000685158   -0.056875100   -0.063729800   0.000000000   0.000000000   0.133973000   0.218136000   0.176183000   -0.038661200   0.060308000   -0.033402300   0.000000000   0.180630000   0.289652000   0.220859000   0.341358000
    0.07   3.781630000   -1.461340000   0.225844000   6.620190000   0.001175680   -0.043052000   -0.049789000   0.000000000   0.000000000   0.139714000   0.206862000   0.145621000   -0.038893400   0.071260300   -0.027363900   0.000000000   0.194176000   0.296609000   0.235714000   0.354515000
    0.10   3.792600000   -1.414410000   0.208667000   6.892480000   0.001601790   -0.058451800   -0.064433500   0.000000000   0.000000000   0.155236000   0.210168000   0.156052000   -0.019545700   0.084246100   -0.022831500   0.000000000   0.181926000   0.306918000   0.244969000   0.356785000
    0.15   3.778380000   -1.293440000   0.163550000   6.717350000   0.002028820   -0.035863600   -0.091537900   0.085537200   0.000000000   0.158937000   0.199726000   0.186495000   -0.020557800   0.074269000   -0.026728700   0.000000000   0.181380000   0.305998000   0.241833000   0.355716000
    0.20   3.692760000   -1.181950000   0.119101000   5.786590000   0.002122900    0.067201900   -0.091505400   0.145251000   0.000000000   0.138968000   0.216584000   0.199500000    0.018953200   0.133352000    0.026665200   0.000000000   0.177903000   0.300131000   0.219913000   0.348896000
    0.26   3.676100000   -1.165490000   0.102609000   5.451920000   0.001653610    0.129716000   -0.097514500   0.135986000   0.000000000   0.126737000   0.249141000   0.229736000    0.023562700   0.143428000    0.039233500   0.000000000   0.178211000   0.300652000   0.200662000   0.349501000
    0.30   3.669660000   -1.175200000   0.099164000   5.407320000   0.001247800    0.145499000   -0.104880000   0.135159000   0.000000000   0.113881000   0.259274000   0.252504000    0.018438300   0.138662000    0.043489300   0.000000000   0.184254000   0.295463000   0.193285000   0.348207000
    0.36   3.597210000   -1.144790000   0.095007700   5.020640000   0.000918966    0.168179000   -0.114223000   0.149582000   0.000000000   0.109638000   0.274211000   0.282686000    0.012675100   0.122472000    0.036661700   0.000000000   0.184085000   0.295192000   0.187569000   0.347887000
    0.40   3.556710000   -1.145200000   0.094317300   5.080660000   0.000672779    0.173884000   -0.120149000   0.151849000   0.000000000   0.110223000   0.280836000   0.301657000    0.022149900   0.129181000    0.046122800   0.000000000   0.191734000   0.292878000   0.180758000   0.350056000
    0.46   3.501770000   -1.130800000   0.100456000   4.957770000   0.000583160    0.190813000   -0.123177000   0.130847000   0.000000000   0.108079000   0.298022000   0.347080000    0.017164500   0.115968000    0.044778200   0.000000000   0.199690000   0.291096000   0.182941000   0.353006000
    0.50   3.457170000   -1.116310000   0.101994000   4.698770000   0.000508794    0.203522000   -0.126077000   0.122339000   0.000000000   0.108783000   0.305295000   0.370989000    0.016711700   0.114252000    0.049822200   0.000000000   0.200063000   0.291640000   0.175988000   0.353665000
    0.60   3.387990000   -1.104700000   0.104529000   4.546430000   0.000249318    0.242603000   -0.126011000   0.095964800   0.000000000   0.106929000   0.321296000   0.440581000    0.013694500   0.100223000    0.042017600   0.000000000   0.207756000   0.289459000   0.176453000   0.356299000
    0.70   3.343810000   -1.116090000   0.099989200   4.640170000   0.000000000    0.280922000   -0.124614000   0.092047500   0.000000000   0.102965000   0.331801000   0.503562000    0.024399300   0.092189300    0.049608600   0.000000000   0.208828000   0.290952000   0.178954000   0.358137000
    0.80   3.258020000   -1.109070000   0.119754000   4.638490000   0.000000000    0.291242000   -0.122604000   0.032747700   0.000000000   0.097480900   0.341281000   0.542709000    0.024482700   0.078739400    0.049226200   0.000000000   0.211136000   0.294168000   0.180310000   0.362096000
    0.90   3.168990000   -1.087140000   0.117879000   4.504810000   0.000000000    0.311362000   -0.123730000   0.052576100   0.000000000   0.087056700   0.342803000   0.581633000    0.042375500   0.091253700    0.068451600   0.000000000   0.220213000   0.293618000   0.194549000   0.367022000
    1.00   3.146490000   -1.093870000   0.114285000   4.531180000   0.000000000    0.359324000   -0.117738000   0.044584200   0.000000000   0.086495700   0.345210000   0.590175000    0.053679200   0.091382100    0.067455400   0.000000000   0.221524000   0.295365000   0.196091000   0.369206000
    1.30   2.895150000   -1.030420000   0.136666000   4.532080000   0.000000000    0.393471000   -0.115441000   0.000000000   0.000000000   0.092091300   0.345292000   0.618805000    0.087972000   0.119863000    0.100768000   0.000000000   0.222493000   0.296657000   0.196817000   0.370822000
    1.50   2.763660000   -1.014370000   0.144100000   4.611720000   0.000000000    0.432513000   -0.104296000   0.000000000   0.000000000   0.103385000   0.342842000   0.653192000    0.123393000   0.165217000    0.143638000   0.000000000   0.218105000   0.303878000   0.198490000   0.374047000
    1.80   2.636620000   -1.048380000   0.180838000   5.396070000   0.000000000    0.434162000   -0.096297900   0.000000000   0.000000000   0.107251000   0.333706000   0.618956000    0.161886000   0.193198000    0.201695000   0.000000000   0.212905000   0.310360000   0.201126000   0.376367000
    2.00   2.621500000   -1.054300000   0.181367000   5.567720000   0.000000000    0.458752000   -0.095576300   0.000000000   0.000000000   0.099358000   0.329709000   0.604177000    0.139794000   0.167929000    0.185814000   0.000000000   0.222240000   0.309638000   0.202676000   0.381138000
    2.60   2.463180000   -1.073080000   0.226407000   6.234910000   0.000000000    0.475305000   -0.078811800   0.000000000   0.000000000   0.105913000   0.312454000   0.577657000    0.125695000   0.153396000    0.173281000   0.000000000   0.223041000   0.310755000   0.207080000   0.382513000
    3.00   2.396800000   -1.057060000   0.248126000   6.767400000   0.000000000    0.481080000   -0.071968900   0.000000000   0.000000000   0.127642000   0.318684000   0.597588000    0.052424200   0.047118500    0.116645000   0.000000000   0.236576000   0.302186000   0.212410000   0.383777000
    """)


class BindiEtAl2014RjbEC8NoSOF(BindiEtAl2014RjbEC8):
    """
    Implements the Bindi et al (2014) GMPE for the case in which
    the site amplification is defined according to the Eurocode 8
    classification, but style-of-faulting is neglected
    """
    #: Required rupture parameters are magnitude
    REQUIRES_RUPTURE_PARAMETERS = {'mag'}
    sof = False


class BindiEtAl2014Rhyp(BindiEtAl2014Rjb):
    """
    Implements the Bindi et al (2014) GMPE for the case in which hypocentral
    distance is preferred, style-of-faulting is specfieid and for which the
    site amplification is dependent directly on Vs30
    """
    #: Required distance measure is Rhypo (eq. 1).
    REQUIRES_DISTANCES = {'rhypo'}

    #: Coefficients from Table 4
    COEFFS = CoeffsTable(sa_damping=5, table="""
    imt             e1             c1             c2             h             c3            b1             b2            b3          gamma           sofN           sofR           sofS           tau           phi        phis2s         sigma
    pgv    3.242490000   -1.575560000    0.079177400   4.389180000   0.0000000000   0.472433000   -0.072548400   0.436952000   -0.508833000   -0.015719500    0.071385900   -0.055666000   0.193206000   0.295126000   0.178867000   0.352744000
    pga    4.273910000   -1.578210000    0.108218000   4.827430000   0.0000963923   0.217109000   -0.068256300   0.352976000   -0.293242000   -0.047214500    0.110979000   -0.063763900   0.145783000   0.291566000   0.186662000   0.325981000
    0.02   4.339700000   -1.604020000    0.103401000   4.478520000   0.0000263293   0.230422000   -0.066535400   0.363906000   -0.286524000   -0.046923100    0.115063000   -0.068140000   0.154538000   0.290986000   0.188250000   0.329477000
    0.04   4.468390000   -1.685360000    0.126703000   4.580630000   0.0000000000   0.205651000   -0.052810200   0.323734000   -0.232462000   -0.045172300    0.114597000   -0.069425000   0.158402000   0.298261000   0.192664000   0.337714000
    0.07   4.572400000   -1.638630000    0.123954000   5.120960000   0.0007222300   0.226272000   -0.029801500   0.311109000   -0.195629000   -0.053205000    0.121653000   -0.068447700   0.169775000   0.302117000   0.205229000   0.346552000
    0.10   4.552550000   -1.579470000    0.125609000   5.675110000   0.0012390400   0.167382000   -0.050906600   0.348968000   -0.168432000   -0.047039300    0.119021000   -0.071982100   0.165148000   0.310963000   0.212643000   0.352097000
    0.15   4.511190000   -1.447100000    0.084609700   4.824800000   0.0016920200   0.194714000   -0.078450700   0.448903000   -0.194539000   -0.036312300    0.102481000   -0.066168600   0.145533000   0.310621000   0.216313000   0.343023000
    0.20   4.495710000   -1.370390000    0.038535800   4.569650000   0.0015859300   0.289627000   -0.081549900   0.533244000   -0.270912000   -0.038675400    0.107555000   -0.068879300   0.144701000   0.308845000   0.202040000   0.341063000
    0.26   4.492240000   -1.366790000    0.012937400   3.948020000   0.0010587800   0.321065000   -0.104184000   0.596455000   -0.323555000   -0.036577100    0.103236000   -0.066658900   0.156869000   0.313737000   0.199484000   0.350769000
    0.30   4.517260000   -1.400780000    0.001979970   4.268160000   0.0005648190   0.336096000   -0.115261000   0.612107000   -0.363199000   -0.038065000    0.104818000   -0.066753200   0.165195000   0.311052000   0.186722000   0.352197000
    0.36   4.465590000   -1.409730000    0.000488761   4.399780000   0.0000596605   0.346351000   -0.127114000   0.600314000   -0.430464000   -0.028534300    0.095509300   -0.066974900   0.164907000   0.310509000   0.180734000   0.351583000
    0.40   4.468340000   -1.428930000   -0.009095590   4.603900000   0.0000000000   0.353351000   -0.137776000   0.621323000   -0.467397000   -0.026162600    0.097198300   -0.071035500   0.165146000   0.310959000   0.182064000   0.352092000
    0.46   4.371500000   -1.406550000    0.001009530   4.602540000   0.0000000000   0.357170000   -0.142768000   0.589127000   -0.531694000   -0.019281900    0.090202000   -0.070919800   0.181401000   0.306033000   0.176797000   0.355756000
    0.50   4.341980000   -1.397510000    0.004238030   4.430450000   0.0000000000   0.384532000   -0.140916000   0.543301000   -0.555531000   -0.017579800    0.086012300   -0.068432100   0.189686000   0.304174000   0.178065000   0.358473000
    0.60   4.214950000   -1.379190000    0.013733000   3.696150000   0.0000000000   0.408720000   -0.141998000   0.504772000   -0.627036000    0.001156930    0.071288600   -0.070131400   0.200502000   0.306270000   0.189183000   0.366066000
    0.70   4.148320000   -1.371690000    0.002264110   3.009780000   0.0000000000   0.466754000   -0.138065000   0.498126000   -0.698998000    0.010002700    0.054387600   -0.064390000   0.201810000   0.308270000   0.264361000   0.368453000
    0.80   4.092460000   -1.377360000    0.008956000   3.157270000   0.0000000000   0.510102000   -0.132630000   0.437529000   -0.757522000    0.015018400    0.045864700   -0.060882800   0.211664000   0.308550000   0.208994000   0.374172000
    0.90   4.083240000   -1.386490000   -0.004531510   3.453700000   0.0000000000   0.567727000   -0.127244000   0.458110000   -0.786632000    0.016380200    0.044223600   -0.060603500   0.225279000   0.313873000   0.225906000   0.386351000
    1.00   4.072070000   -1.387350000   -0.018545800   3.316300000   0.0000000000   0.631338000   -0.121241000   0.474982000   -0.791438000    0.026395700    0.041136600   -0.067531900   0.238973000   0.318631000   0.246861000   0.398289000
    1.30   3.779540000   -1.273430000   -0.013766200   3.049760000   0.0000000000   0.650829000   -0.129005000   0.488244000   -0.803656000    0.024922000    0.038329000   -0.063250700   0.212162000   0.324083000   0.245588000   0.387354000
    1.50   3.694470000   -1.264770000   -0.003373340   3.654820000   0.0000000000   0.674600000   -0.119081000   0.461122000   -0.780198000    0.019123100    0.038696600   -0.057819500   0.208441000   0.334250000   0.244150000   0.393917000
    1.80   3.454080000   -1.273640000    0.083746000   4.599880000   0.0000000000   0.563304000   -0.117803000   0.184126000   -0.749008000    0.011675900    0.029249000   -0.040924700   0.203238000   0.342873000   0.256308000   0.398582000
    2.00   3.389010000   -1.282830000    0.086724000   4.952850000   0.0000000000   0.548353000   -0.129571000   0.171017000   -0.744073000    0.004992770    0.033587300   -0.038579800   0.205751000   0.347114000   0.261830000   0.403511000
    2.60   3.066010000   -1.234270000    0.150146000   4.455110000   0.0000000000   0.541750000   -0.103699000   0.009302580   -0.744468000    0.006026810    0.030508100   -0.036534700   0.190711000   0.339373000   0.242015000   0.389288000
    3.00   2.893910000   -1.164610000    0.162354000   4.623210000   0.0000000000   0.590765000   -0.085328600   0.034058400   -0.693999000    0.018621100   -0.018982400    0.000361328   0.183363000   0.326297000   0.228650000   0.374289000
    """)


class BindiEtAl2014RhypEC8(BindiEtAl2014RjbEC8):
    """
    Implements the Bindi et al (2014) GMPE for the case in which hypocentral
    distance is preferred, style-of-faulting is specfied and site amplification
    is characterised according to the Eurocode 8 site class
    """
    #: Required distance measure is Rhypo
    REQUIRES_DISTANCES = {'rhypo'}

    #: Coefficients from Table 3
    COEFFS = CoeffsTable(sa_damping=5, table="""
    imt             e1             c1            c2             h             c3            b1             b2            b3            eA            eB            eC            eD           sofN           sofR           sofS          sofU           tau           phi        phis2s         sigma
    pgv    3.292610000   -1.665480000   0.136478000   6.310130000   0.0000000000   0.436373000   -0.049720200   0.264336000   0.000000000   0.130319000   0.272298000   0.350870000   -0.090869900    0.013282500   -0.067381500   0.000000000   0.241933000   0.284305000   0.231138000   0.373311000
    pga    4.366930000   -1.752120000   0.150507000   7.321920000   0.0000000000   0.144291000   -0.066081100   0.284211000   0.000000000   0.143778000   0.231064000   0.187402000   -0.071745100    0.084957800   -0.057096500   0.000000000   0.195249000   0.284622000   0.213455000   0.345155000
    0.02   4.420440000   -1.777540000   0.147715000   7.064280000   0.0000000000   0.147874000   -0.066205600   0.297090000   0.000000000   0.141110000   0.225339000   0.187033000   -0.065306900    0.091731900   -0.056125500   0.000000000   0.197407000   0.287767000   0.216309000   0.348969000
    0.04   4.549920000   -1.854600000   0.165968000   6.982270000   0.0000000000   0.124402000   -0.056602000   0.260601000   0.000000000   0.140350000   0.217010000   0.146507000   -0.065379200    0.088098100   -0.057670900   0.000000000   0.204345000   0.297881000   0.222929000   0.361234000
    0.07   4.732850000   -1.878220000   0.157048000   8.133700000   0.0000000000   0.138028000   -0.040786500   0.276090000   0.000000000   0.145543000   0.206101000   0.115846000   -0.051289600    0.113143000   -0.037623000   0.000000000   0.208843000   0.304438000   0.242821000   0.369185000
    0.10   4.675030000   -1.799170000   0.151808000   8.380980000   0.0005478660   0.098832300   -0.056937000   0.322027000   0.000000000   0.158622000   0.208849000   0.125428000   -0.037486800    0.120065000   -0.036904000   0.000000000   0.195390000   0.313320000   0.251339000   0.369252000
    0.15   4.569650000   -1.614050000   0.105601000   7.496250000   0.0011834100   0.125747000   -0.083500900   0.464456000   0.000000000   0.162534000   0.197589000   0.158161000   -0.047089600    0.098045600   -0.050605600   0.000000000   0.193856000   0.310861000   0.247987000   0.366353000
    0.20   4.450170000   -1.465010000   0.056754500   6.272220000   0.0014308100   0.236642000   -0.083463900   0.542025000   0.000000000   0.143446000   0.213637000   0.170195000   -0.021448300    0.139454000   -0.012459600   0.000000000   0.191231000   0.306652000   0.226544000   0.361392000
    0.26   4.455930000   -1.443420000   0.032061300   5.480400000   0.0009816830   0.313239000   -0.089717600   0.555789000   0.000000000   0.133443000   0.244854000   0.202162000   -0.030488000    0.132769000   -0.015155100   0.000000000   0.192222000   0.308241000   0.214042000   0.363266000
    0.30   4.471710000   -1.460160000   0.025927200   5.503160000   0.0005543760   0.332549000   -0.097217900   0.551296000   0.000000000   0.121637000   0.254554000   0.226009000   -0.042269100    0.119803000   -0.019226600   0.000000000   0.199096000   0.304125000   0.207111000   0.363499000
    0.36   4.387990000   -1.418420000   0.022150300   4.769520000   0.0002687480   0.355357000   -0.106041000   0.543724000   0.000000000   0.118062000   0.268087000   0.258058000   -0.056669000    0.092863000   -0.034960300   0.000000000   0.199491000   0.304728000   0.201784000   0.364220000
    0.40   4.376090000   -1.428430000   0.016902400   4.819740000   0.0000000000   0.368987000   -0.111955000   0.547881000   0.000000000   0.119481000   0.275041000   0.275672000   -0.053267600    0.091980000   -0.032188300   0.000000000   0.207716000   0.302796000   0.194828000   0.367194000
    0.46   4.333720000   -1.425030000   0.025903300   5.109610000   0.0000000000   0.379142000   -0.115152000   0.511833000   0.000000000   0.117659000   0.291964000   0.321124000   -0.062509500    0.073772300   -0.039294000   0.000000000   0.216313000   0.301380000   0.197633000   0.370974000
    0.50   4.293590000   -1.414650000   0.028367500   4.955190000   0.0000000000   0.389410000   -0.118151000   0.495459000   0.000000000   0.118871000   0.298870000   0.344584000   -0.064737900    0.069448700   -0.037414200   0.000000000   0.225415000   0.300553000   0.198934000   0.375691000
    0.60   4.239150000   -1.406030000   0.026979900   4.635970000   0.0000000000   0.430341000   -0.119284000   0.475308000   0.000000000   0.117717000   0.314097000   0.412316000   -0.076075300    0.045870600   -0.054880500   0.000000000   0.234484000   0.299514000   0.208675000   0.380383000
    0.70   4.196960000   -1.412970000   0.020875700   4.293770000   0.0000000000   0.470648000   -0.118095000   0.460014000   0.000000000   0.115734000   0.325887000   0.477053000   -0.074956400    0.028574500   -0.055644400   0.000000000   0.246498000   0.301897000   0.212696000   0.389747000
    0.80   4.114530000   -1.404290000   0.038146400   4.010590000   0.0000000000   0.481962000   -0.116743000   0.393948000   0.000000000   0.110981000   0.334461000   0.517530000   -0.081627800    0.008428810   -0.063434400   0.000000000   0.249844000   0.305995000   0.224068000   0.395038000
    0.90   4.032490000   -1.389770000   0.037093500   3.978120000   0.0000000000   0.504043000   -0.116645000   0.400442000   0.000000000   0.103765000   0.334934000   0.559004000   -0.064291400    0.019498400   -0.045615800   0.000000000   0.261433000   0.307220000   0.240384000   0.403399000
    1.00   4.011400000   -1.395430000   0.034061400   4.096680000   0.0000000000   0.550001000   -0.110860000   0.386023000   0.000000000   0.103026000   0.336196000   0.566463000   -0.057167500    0.014892500   -0.051388400   0.000000000   0.274446000   0.309616000   0.244465000   0.413742000
    1.30   3.684020000   -1.302310000   0.069534500   3.732900000   0.0000000000   0.544404000   -0.113618000   0.282169000   0.000000000   0.108865000   0.337519000   0.592894000   -0.034663900    0.029823500   -0.025078900   0.000000000   0.265310000   0.311777000   0.244067000   0.409383000
    1.50   3.535870000   -1.273510000   0.082245800   4.074080000   0.0000000000   0.570581000   -0.103758000   0.249760000   0.000000000   0.119032000   0.333110000   0.626267000   -0.010667700    0.060266600    0.007385850   0.000000000   0.269363000   0.316539000   0.236824000   0.415637000
    1.80   3.465880000   -1.361020000   0.137018000   6.097100000   0.0000000000   0.524014000   -0.101089000   0.046975200   0.000000000   0.123814000   0.323505000   0.600530000   -0.002974540    0.058459200    0.039470900   0.000000000   0.275390000   0.323622000   0.257636000   0.424936000
    2.00   3.469100000   -1.381110000   0.137878000   6.539170000   0.0000000000   0.551312000   -0.098766100   0.000000000   0.000000000   0.115091000   0.320404000   0.586654000   -0.023796000    0.034963600    0.025270300   0.000000000   0.277179000   0.325724000   0.259839000   0.427696000
    2.60   3.283840000   -1.389770000   0.188643000   7.040110000   0.0000000000   0.547984000   -0.084231400   0.000000000   0.000000000   0.124833000   0.306133000   0.548523000   -0.050663600    0.003435150    0.007395600   0.000000000   0.278908000   0.327756000   0.263531000   0.430364000
    3.00   3.264700000   -1.399740000   0.216533000   8.339210000   0.0000000000   0.552993000   -0.071343600   0.000000000   0.000000000   0.143969000   0.315187000   0.559213000   -0.146666000   -0.128655000   -0.067567300   0.000000000   0.283885000   0.320266000   0.267078000   0.427973000
    4.00   3.051920000   -1.333280000   0.203724000   8.409960000   0.0000000000   0.652840000   -0.054790600   0.000000000   0.000000000   0.124787000   0.285654000   0.532224000   -0.141040000   -0.153993000   -0.059989300   0.000000000   0.259933000   0.305458000   0.000000000   0.401086000
    """)


class BindiEtAl2014RhypEC8NoSOF(BindiEtAl2014RhypEC8):
    """
    Implements the Bindi et al. (2014) GMPE for the case in which
    hypocentral distance is preferred, Eurocode 8 site amplification is used
    and style-of-faulting is unspecfied.
    """
    #: Required rupture parameters are magnitude
    REQUIRES_RUPTURE_PARAMETERS = {'mag'}
    sof = False
