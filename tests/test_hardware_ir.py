import json
import xml.etree.ElementTree as ET

from logix_mcp.hardware import (
    build_device_tree,
    extract_hardware_ir,
    io_points,
    io_tags,
    module_connections,
    module_elements,
    module_ir,
)


HARDWARE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content>
  <Controller Name="Demo">
    <Modules>
      <Module Name="Local" CatalogNumber="1756-L85E" ParentModule="Local" ParentModPortId="1" Inhibited="false">
        <EKey State="Disabled"/>
        <Ports>
          <Port Id="1" Address="0" Type="ICP" Upstream="false">
            <Bus Size="2"/>
          </Port>
          <Port Id="2" Type="Ethernet" Upstream="false">
            <Bus/>
          </Port>
        </Ports>
      </Module>
      <Module Name="AENTR" CatalogNumber="1734-AENTR/B" ParentModule="Local" ParentModPortId="2" Inhibited="true">
        <Ports>
          <Port Id="1" Address="0" Type="PointIO" Upstream="false">
            <Bus Size="2"/>
          </Port>
          <Port Id="2" Address="192.168.1.11" Type="Ethernet" Upstream="true"/>
        </Ports>
        <Communications CommMethod="805306369">
          <Connections>
            <Connection Name="Output" RPI="20000" Type="Output">
              <InputTag ExternalAccess="Read/Write">
                <Data Format="Decorated">
                  <Structure DataType="AB:1734_2SLOT:I:0"/>
                </Data>
              </InputTag>
              <OutputTag ExternalAccess="Read/Write">
                <Data Format="Decorated">
                  <Structure DataType="AB:1734_2SLOT:O:0"/>
                </Data>
              </OutputTag>
            </Connection>
          </Connections>
        </Communications>
      </Module>
      <Module CatalogNumber="1734-IB8/C" ParentModule="AENTR" ParentModPortId="1" Inhibited="false">
        <Ports>
          <Port Id="1" Address="1" Type="PointIO" Upstream="true"/>
        </Ports>
        <Communications>
          <ConfigTag ConfigSize="36" ExternalAccess="Read/Write">
            <Data Format="Decorated">
              <Structure DataType="AB:1734_DI8:C:0"/>
            </Data>
          </ConfigTag>
          <Connections>
            <RackConnection>
              <InAliasTag>
                <Description><![CDATA[SLOT 1 INPUTS]]></Description>
                <Comments>
                  <Comment Operand=".0"><![CDATA[E-STOP]]></Comment>
                  <Comment Operand=".7"><![CDATA[READY]]></Comment>
                </Comments>
              </InAliasTag>
            </RackConnection>
          </Connections>
        </Communications>
      </Module>
      <Module CatalogNumber="1734-OB8/C" ParentModule="AENTR" ParentModPortId="1" Inhibited="false">
        <Ports>
          <Port Id="1" Address="2" Type="PointIO" Upstream="true"/>
        </Ports>
        <Communications>
          <Connections>
            <RackConnection>
              <InAliasTag/>
              <OutAliasTag>
                <Description><![CDATA[SLOT 2 OUTPUTS]]></Description>
                <Comments>
                  <Comment Operand=".1"><![CDATA[PILOT LIGHT]]></Comment>
                </Comments>
              </OutAliasTag>
            </RackConnection>
          </Connections>
        </Communications>
      </Module>
    </Modules>
  </Controller>
</RSLogix5000Content>
"""


def _modules() -> list[ET.Element]:
    return module_elements(ET.fromstring(HARDWARE_XML))


def test_module_ir_normalizes_ports_connections_and_io_tags():
    modules = _modules()

    adapter = module_ir(modules[1], 1)
    assert adapter["name"] == "AENTR"
    assert adapter["slot"] == "0"
    assert adapter["network_address"] == "192.168.1.11"
    assert adapter["ports"][0]["bus"]["size"] == "2"

    adapter_connections = module_connections(modules[1], 1)
    assert adapter_connections[0]["kind"] == "connection"
    assert adapter_connections[0]["name"] == "Output"
    assert [tag["role"] for tag in adapter_connections[0]["io_tags"]] == ["Input", "Output"]

    input_module = module_ir(modules[2], 2)
    assert input_module["name"] == "AENTR:1"
    assert input_module["name_source"] == "generated"
    assert input_module["slot"] == "1"

    tags = io_tags(modules[2], 2)
    assert tags[0]["role"] == "Config"
    assert tags[0]["data_type"] == "AB:1734_DI8:C:0"
    assert tags[1]["role"] == "InAlias"
    assert tags[1]["comment_count"] == 2


def test_io_points_extract_comment_operands_with_direction():
    modules = _modules()

    input_points = io_points(modules[2], 2)
    assert input_points == [
        {
            "module": "AENTR:1",
            "module_id": "Module:AENTR:1",
            "module_catalog_number": "1734-IB8/C",
            "parent_module": "AENTR",
            "slot": "1",
            "role": "InAlias",
            "direction": "input",
            "operand": ".0",
            "point": 0,
            "description": "E-STOP",
            "tag_description": "SLOT 1 INPUTS",
        },
        {
            "module": "AENTR:1",
            "module_id": "Module:AENTR:1",
            "module_catalog_number": "1734-IB8/C",
            "parent_module": "AENTR",
            "slot": "1",
            "role": "InAlias",
            "direction": "input",
            "operand": ".7",
            "point": 7,
            "description": "READY",
            "tag_description": "SLOT 1 INPUTS",
        },
    ]

    output_points = io_points(modules[3], 3)
    assert output_points[0]["role"] == "OutAlias"
    assert output_points[0]["direction"] == "output"
    assert output_points[0]["point"] == 1
    assert output_points[0]["description"] == "PILOT LIGHT"


def test_extract_hardware_ir_builds_tree_and_is_json_serializable():
    root = ET.fromstring(HARDWARE_XML)
    ir = extract_hardware_ir(root)

    assert len(ir["modules"]) == 4
    assert len(ir["module_connections"]) == 3
    assert len(ir["io_points"]) == 3

    tree = ir["device_tree"]
    assert tree["roots"][0]["name"] == "Local"
    adapter = tree["roots"][0]["children"][0]
    assert adapter["name"] == "AENTR"
    assert [child["name"] for child in adapter["children"]] == ["AENTR:1", "AENTR:2"]

    rebuilt_tree = build_device_tree(ir["modules"])
    assert rebuilt_tree == tree
    json.dumps(ir)
