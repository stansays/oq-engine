# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4
"""
This modules serializes objects in shaml format.

It currently takes all the lxml object model in memory
due to the fact that curves can be grouped by IDmodel and
IML. Couldn't find a way to do so writing an object at a
time without making a restriction to the order on which
objects are received.

"""

from lxml import etree

from opengem import writer
from opengem.xml import NSMAP, SHAML, GML, NRML

class HazardCurveXMLWriter(writer.FileWriter):
    """This class writes an hazard curve into the shaml format."""

    def __init__(self, path):
        super(HazardCurveXMLWriter, self).__init__(path)
        self.result_list_tag = etree.Element(SHAML + "HazardResult", 
            nsmap=NSMAP)

        self.curves_per_iml = {}
        self.curves_per_model_id = {}

    def close(self):
        """Overrides the default implementation writing all the
        collected lxml object model to the stream."""

        if not len(self.result_list_tag):
            error = "You need to add at least a curve to build a valid output!"
            raise RuntimeError(error)

        # self.file.write(etree.tostring(self.result_list_tag, 
        #         pretty_print=True,
        #         xml_declaration=True,
        #         encoding="UTF-8"))
                
        et = etree.ElementTree(self.result_list_tag)
        et.write(self.file, pretty_print=True,
                xml_declaration=True,
                encoding="UTF-8")
                
        super(HazardCurveXMLWriter, self).close()

    def _add_curve_to_proper_set(self, point, values, values_tag):
        """Adds the curve to the proper set depending on the IML values."""
        
        try:
            list_tag = self.curves_per_iml[str(values["IMLValues"])]
            
        except KeyError:

            # <shaml:Hazard Curve />
            list_tag = etree.SubElement(values_tag, SHAML + "HazardCurve")
            
            # <gml:pos />
            gml_tag = etree.SubElement(list_tag, GML + "pos")
            gml_tag.text = " ".join(map(str, (point.longitude, 
                point.latitude)))
            
            # <shaml:values />
            iml_values_tag = etree.SubElement(list_tag, SHAML + "Values")
            iml_values_tag.text = " ".join(map(str, values["Values"]))
            
            # <shaml:IML values />
            iml_values_tag = etree.SubElement(values_tag, SHAML + "IMLValues")
            iml_values_tag.text = " ".join(map(str, values["IMLValues"]))
            
            self.curves_per_iml[str(values["IMLValues"])] = list_tag
            
    def write(self, point, values):
        """Writes a hazard curve.
        
        point must be of type shapes.Site
        values is a dictionary that matches the one produced by the
        parser shaml_output.ShamlOutputFile
        
        """
        try:
            list_tag = self.curves_per_model_id[values["IDmodel"]]
        except KeyError:
        
            # <shaml:Result />
            config_tag = etree.SubElement(
                    self.result_list_tag, SHAML + "Config")
                
            list_tag = etree.SubElement(
                    self.result_list_tag, SHAML + "HazardCurveList")
            list_tag.attrib["endBranchLabel"] = str(values["endBranchLabel"])

            # <shaml:HazardProcessing />
            HazardProcessing_tag = etree.SubElement(config_tag, SHAML + 
                "HazardProcessing")
            HazardProcessing_tag.attrib["timeSpanDuration"] = str(values
                ["timeSpanDuration"])
            HazardProcessing_tag.attrib["IDmodel"] = str(values["IDmodel"])
            HazardProcessing_tag.attrib["saPeriod"] = str(values["saPeriod"])
            HazardProcessing_tag.attrib["saDamping"] = str(values["saDamping"])
        
             # <shaml:IML values />
            imt_tag = etree.SubElement(HazardProcessing_tag, SHAML + "IMT")
            imt_tag.text = str(values["IMT"])
            
            # <shaml:Values />
            values_tag = etree.SubElement(config_tag, SHAML + "Values")
            
            self.curves_per_model_id[values["IDmodel"]] = values_tag
            
        self._add_curve_to_proper_set(point, values, list_tag)
