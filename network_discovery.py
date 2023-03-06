
from ryu.base import app_manager
from ryu.base.app_manager import lookup_service_brick
from ryu.controller.handler import set_ev_cls
from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link
from ryu.lib import hub

from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import DEAD_DISPATCHER

from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4
from ryu.lib.packet import arp
from ryu.lib.packet import icmp
from ryu.lib import hub

import networkx as nx

import setting, time, json, os


class NetworkDiscovery(app_manager.RyuApp):
    
    # List the event list should be listened.
    events = [event.EventSwitchEnter,
              event.EventSwitchLeave, event.EventPortAdd,
              event.EventPortDelete, event.EventPortModify,
              event.EventLinkAdd, event.EventLinkDelete]

    def __init__(self, *args, **kwargs):
        super(NetworkDiscovery, self).__init__(*args, **kwargs)
        
        self.name='discovery'

        self.link_to_port = {}                # {(src_dpid,dst_dpid):(src_port,dst_port),}
        self.access_table = {}                # {(sw,port):(ip, mac),}
        self.switch_mac_table = {}            # {sw: [mac, mac, ...]}
        self.arp_table = {}                   # {ip:mac}
        self.rev_arp_table = {}               # {mac:ip}

        
        #Added by HAMED: For testing 32 nodes Scenario
        # for i in range(1,33):
        #     self.access_table[(i,1)]=(f'10.0.0.{i:02d}', f'00:00:00:00:00:{i:02x}')

        self.links = []
        self.switches = []                    # self.switches = [dpid,]
        
        self.switch_port_table = {}
        self.switch_interior_port_table = {}
        self.switch_access_port_table = {}

        self.graph = nx.DiGraph()

        # Get initiation delay.
        self.initiation_delay = 30
        self.start_time = time.time()

        # Start a thread to discover network resource.
        self.discover_thread = hub.spawn(self._discover)
    
    def _discover(self):
        while True:                       
            self.get_topology_data(None)
            hub.sleep(setting.DISCOVERY_PERIOD)
    
    @set_ev_cls(events)
    def get_topology_data(self, ev):
        
        switch_list = get_switch(self, None)
        
        self.switches=[switch for switch in switch_list]
        
        links_list = get_link(self, None)
        
        self.links=[(link.src.dpid,link.dst.dpid) for link in links_list]
        
        self.link_to_port={(link.src.dpid,link.dst.dpid):(link.src.port_no,link.dst.port_no) for link in links_list}
        
        self.graph = self.get_graph(self.links)
        
        self.switch_port_table={sw.dp.id:set(port.port_no for port in sw.ports) for sw in self.switches}
        
        for sw in switch_list:
            dpid = sw.dp.id
            
            self.switch_interior_port_table.setdefault(dpid, set())
            
            self.switch_access_port_table.setdefault(dpid, set())

        self.switch_interior_port_table = self.create_interior_port_table(links_list)
        
        self.switch_access_port_table = self.create_access_port_table()

        
        # get this once for topology and no more
        graph_dict = nx.to_dict_of_dicts(self.graph)

        if len(self.switches) == setting.NUMBER_OF_NODES:

            file_graph = setting.PATH_TO_FILES+'/DRL/32nodes/graph_'+str(len(self.switches))+'Nodes.json'
            os.makedirs(os.path.dirname(file_graph), exist_ok=True)
            with open(file_graph,'w') as json_file:
                json.dump(graph_dict, json_file, indent=2)
        



        # print('topology',graph_dict)

        # self.shortest_paths = self.get_k_paths() 
        # k shorthest paths for drl--> removed from C0 since huge CPU consumptio
        # Now I calculate k_spaths outside, the agent just know it 
        # self.shortest_paths = self.all_k_shortest_paths(
        #     self.graph, weight='weight', k=1)

        self.logger.info("[Network Discovery Ok]")
        
        if setting.TOSHOW:
            self.logger.info (f"Switches:{self.switches}")
            
            self.logger.info (f"=====================")
            self.logger.info (f"Links:{self.links}")
            
            self.logger.info (f"=====================")
            self.logger.info (f"Link_to_port:{self.link_to_port}")
            
            self.logger.info (f"=====================")
            self.logger.info (f"graph:{nx.node_link_data(self.graph)}")

            
            self.logger.info (f"=====================")
            self.logger.info (f"switch_port_table:{self.switch_port_table}")

            self.logger.info (f"=====================")
            self.logger.info (f"switch_interior_port_table:{self.switch_interior_port_table}")

            self.logger.info (f"=====================")
            self.logger.info (f"switch_access_port_table:{self.switch_access_port_table}")
            
            self.logger.info (f"=====================")
            self.logger.info (f"access_table:{self.access_table}")

            self.logger.info (f"=====================")
            self.logger.info (f"arp_table:{self.arp_table}")
            
            self.logger.info (f"=====================")
            self.logger.info (f"rev_arp_table:{self.rev_arp_table}")

            self.logger.info (f"=====================")
            self.logger.info (f"switch_mac_table:{self.switch_mac_table}")
            
    def create_interior_port_table(self, link_list):
        """
            Get links' srouce port to dst port  from link_list.
            link_to_port = {(src_dpid,dst_dpid):(src_port,dst_port),}
        """
        
        for link in link_list:
            if link.src.dpid in [switch.dp.id for switch in self.switches]:
                self.switch_interior_port_table[link.src.dpid].add(link.src.port_no)
            if link.dst.dpid in [switch.dp.id for switch in self.switches]:
                self.switch_interior_port_table[link.dst.dpid].add(link.dst.port_no)

        return self.switch_interior_port_table
    
    def create_access_port_table(self):
        """
            Get ports without link into access_ports. 
            It needs to be invoked after create interior port table method()
        """
        for sw in self.switch_port_table:
            all_port_table = self.switch_port_table[sw]
            interior_port = self.switch_interior_port_table[sw]
            # That comes the access port of the switch.
            self.switch_access_port_table[sw] = all_port_table - interior_port

        return self.switch_access_port_table

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
            Handle the packet_in packet, and register the access info.
        """
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        # eth_type = pkt.get_protocols(ethernet.ethernet)[0].ethertype #delay
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        icmp_pkt = pkt.get_protocol(icmp.icmp)
        
        if icmp_pkt:
            pass

        if arp_pkt:
            arp_src_ip = arp_pkt.src_ip
            src_mac = arp_pkt.src_mac
            # Record the access infomation.
            self.register_access_info(datapath.id, in_port, arp_src_ip, src_mac)
            self.register_switch_mac_table_info(datapath.id, in_port, src_mac)
            # Create arp table {ip:mac}
            if arp_pkt.opcode == arp.ARP_REPLY:
                self.arp_table[arp_src_ip] = src_mac
                self.rev_arp_table[src_mac] = arp_src_ip

        elif ip_pkt:
            ip_src_ip = ip_pkt.src
            eth = pkt.get_protocols(ethernet.ethernet)[0]
            src_mac = eth.src
            # Record the access infomation.
            self.register_access_info(datapath.id, in_port, ip_src_ip, src_mac)
            self.register_switch_mac_table_info(datapath.id, in_port, src_mac)
        else:
            pass

    def get_host_location(self, host_ip):
        """
            Get host location info ((datapath, port)) according to the host ip.
            self.access_table = {(sw,port):(ip, mac),}
        """
        # print('Access table: \n{0}'.format(self.access_table))
        # print(host_ip)
        
        
        for key in self.access_table.keys():
            if self.access_table[key][0] == host_ip:
                return key
        self.logger.info("%s location is not found." % host_ip)
        return None

    def register_access_info(self, dpid, in_port, ip, mac):
        """
            Register access host info into access table.
        """
        if in_port in self.switch_access_port_table[dpid]:
            if (dpid, in_port) in self.access_table:
                if self.access_table[(dpid, in_port)] == (ip, mac):
                    return
                else:
                    self.access_table[(dpid, in_port)] = (ip, mac)
                    return
            else:
                self.access_table.setdefault((dpid, in_port), None)
                self.access_table[(dpid, in_port)] = (ip, mac)
                return
    
    def register_switch_mac_table_info(self, dpid, in_port, mac):
        """
            Register access host info into access table.
        """
        if in_port in self.switch_access_port_table[dpid]:
            if dpid in self.switch_mac_table:
                if mac in self.switch_mac_table[dpid]:
                    return
                else:
                    self.switch_mac_table[dpid].add(mac)
                    return
            else:
                self.switch_mac_table.setdefault(dpid, set())
                self.switch_mac_table[dpid].add(mac)
                return

    def get_graph(self, link_list):
        """
            Get Adjacency matrix from link_to_port.
        """
        _graph = self.graph.copy()
        for src in self.switches:
            for dst in self.switches:
                if src.dp.id == dst.dp.id:
                    _graph.add_edge(src.dp.id, dst.dp.id, weight=0)
                elif (src.dp.id, dst.dp.id) in link_list:
                    _graph.add_edge(src.dp.id, dst.dp.id, weight=1)
                else:
                    pass
        return _graph