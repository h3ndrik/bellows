from bellows.zigbee.zcl import Cluster

class Xiaomi_HearbeatCluster):
    """ Heartbeat information
    """
    cluster_id = 0xff01
    ep_attribute = 'xiaomi_ff01'
    attributes = {
        # Basic Device Information
        0x0001: ('LUMI_battery_voltage', t.uint16_t),
        0x0003: ('LUMI_soc_temp', t.uint16_t),
        0x0040: ('LUMI_state', t.uint8_t),
    }
    server_commands = {}
    client_commands = {}
    
class ManufacturerSpecificCluster(Cluster):
    cluster_id_range = (0xfc00, 0xffff)
    ep_attribute = 'manufacturer_specific'
    name = "Manufacturer Specific"
    attributes = {}
    server_commands = {}
    client_commands = {}
