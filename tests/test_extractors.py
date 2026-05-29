import json
import xml.etree.ElementTree as ET

from logix_mcp.extractors import (
    extract_alarm_message_records,
    extract_data_nodes,
    extract_descriptions_comments,
    extract_produce_consume_info,
    extract_tag_comment_records,
    xml_path,
    xml_path_all,
)


SAMPLE = """<RSLogix5000Content>
  <Controller Name="Demo">
    <Tags>
      <Tag Name="Motor" TagType="Base" DataType="MOTOR_UDT" ExternalAccess="Read/Write">
        <Description><![CDATA[Main motor state]]></Description>
        <Comments>
          <Comment Operand=".Run"><![CDATA[Run feedback]]></Comment>
          <Comment Operand="[0].Fault"><![CDATA[Fault bit]]></Comment>
        </Comments>
        <Data Format="L5K"><![CDATA[[1,2,3]]]></Data>
        <Data Format="Decorated">
          <Structure DataType="MOTOR_UDT">
            <DataValueMember Name="Run" DataType="BOOL" Value="1"/>
            <ArrayMember Name="Faults" DataType="BOOL" Dimensions="2">
              <Element Index="[0]" Value="0"/>
              <Element Index="[1]" Value="1"/>
            </ArrayMember>
          </Structure>
        </Data>
      </Tag>
      <Tag Name="AlarmTag" TagType="Base" DataType="ALARM_DIGITAL" ExternalAccess="Read/Write">
        <Data Format="Alarm">
          <AlarmDigitalParameters Severity="500" AckRequired="true" AssocTag1="Motor.Run" AssocTag2="SPACE"/>
          <AlarmConfig>
            <Messages>
              <Message Type="AM">
                <Text Lang="en-US"><![CDATA[Motor fault active]]></Text>
              </Message>
            </Messages>
            <AlarmClass><![CDATA[MCC]]></AlarmClass>
          </AlarmConfig>
        </Data>
      </Tag>
      <Tag Name="ProducedTag" TagType="Produced" DataType="DINT">
        <ProduceInfo ProduceCount="1" MinimumRPI="0.200" MaximumRPI="536870.900" DefaultRPI="1000" UnicastPermitted="false"/>
      </Tag>
      <Tag Name="ConsumedTag" TagType="Consumed" DataType="DINT">
        <ConsumeInfo Producer="RemotePLC" RemoteTag="RemoteDint" RemoteInstance="0" RPI="20" Unicast="true"/>
      </Tag>
    </Tags>
  </Controller>
</RSLogix5000Content>"""


def root():
    return ET.fromstring(SAMPLE)


def test_xml_path_helpers_match_local_names_and_alternatives():
    doc = root()

    tags = xml_path_all(doc, "Controller/Tags/Tag")
    first_data = xml_path(tags[0], "Data|DefaultData")

    assert len(tags) == 4
    assert first_data is not None
    assert first_data.attrib["Format"] == "L5K"


def test_extract_descriptions_comments_returns_owner_scoped_records():
    tag = xml_path(root(), "Controller/Tags/Tag")
    records = extract_descriptions_comments(tag)

    assert records[0]["kind"] == "description"
    assert records[0]["text"] == "Main motor state"
    assert records[1]["target"] == "Motor.Run"
    assert records[2]["target"] == "Motor[0].Fault"
    json.dumps(records)


def test_extract_data_nodes_preserves_raw_text_and_decorated_tree():
    records = extract_data_nodes(root())
    by_format = {record["format"]: record for record in records}

    assert by_format["L5K"]["raw_text"] == "[1,2,3]"
    decorated = by_format["Decorated"]["parsed"][0]
    assert decorated["element"] == "Structure"
    assert decorated["attributes"]["DataType"] == "MOTOR_UDT"
    assert decorated["children"][1]["element"] == "ArrayMember"
    assert decorated["children"][1]["children"][1]["attributes"]["Value"] == "1"
    json.dumps(records)


def test_extract_alarm_message_records_flattens_alarm_config_messages():
    records = extract_alarm_message_records(root())

    assert records == [
        {
            "kind": "alarm_message",
            "owner": {
                "element": "Tag",
                "name": "AlarmTag",
                "tag_type": "Base",
                "data_type": "ALARM_DIGITAL",
                "attributes": {
                    "Name": "AlarmTag",
                    "TagType": "Base",
                    "DataType": "ALARM_DIGITAL",
                    "ExternalAccess": "Read/Write",
                },
            },
            "tag_name": "AlarmTag",
            "tag_type": "Base",
            "data_type": "ALARM_DIGITAL",
            "alarm_type": "AlarmDigital",
            "alarm_class": "MCC",
            "severity": "500",
            "assoc_tags": ["Motor.Run"],
            "parameters": {
                "Severity": "500",
                "AckRequired": "true",
                "AssocTag1": "Motor.Run",
                "AssocTag2": "SPACE",
            },
            "path": "RSLogix5000Content/Controller[@Name='Demo']/Tags[1]/Tag[@Name='AlarmTag']/Data[@Format='Alarm']",
            "message_type": "AM",
            "lang": "en-US",
            "text": "Motor fault active",
            "message_attributes": {"Type": "AM"},
            "text_attributes": {"Lang": "en-US"},
        }
    ]
    json.dumps(records)


def test_extract_tag_comment_records_returns_qualified_targets():
    records = extract_tag_comment_records(root())

    assert [record["target"] for record in records] == ["Motor.Run", "Motor[0].Fault"]
    assert records[0]["tag_name"] == "Motor"
    json.dumps(records)


def test_extract_produce_consume_info_returns_flat_common_fields():
    records = extract_produce_consume_info(root())
    by_tag = {record["tag_name"]: record for record in records}

    assert by_tag["ProducedTag"]["direction"] == "produced"
    assert by_tag["ProducedTag"]["produce_count"] == "1"
    assert by_tag["ProducedTag"]["rpi"] == "1000"
    assert by_tag["ConsumedTag"]["direction"] == "consumed"
    assert by_tag["ConsumedTag"]["producer"] == "RemotePLC"
    assert by_tag["ConsumedTag"]["remote_tag"] == "RemoteDint"
    json.dumps(records)
