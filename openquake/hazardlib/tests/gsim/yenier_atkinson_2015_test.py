# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (C) 2021 GEM Foundation
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

from openquake.hazardlib.gsim.yenier_atkinson_2015 import (
    YenierAtkinson2015BSSA)
from openquake.hazardlib.tests.gsim.utils import BaseGSIMTestCase


class YenierAtkinson2015BSSA(BaseGSIMTestCase):

    GSIM_CLASS = YenierAtkinson2015BSSA

    def test_mean(self):
        self.check_all('YA15/ya15_mean_cena.csv', mean_discrep_percentage=0.3)
