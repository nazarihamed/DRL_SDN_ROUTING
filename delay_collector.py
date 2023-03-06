from __future__ import division
from asyncio.log import logger
from ryu import cfg
from ryu.base import app_manager
from ryu.base.app_manager import lookup_service_brick
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.topology.api import get_switch
from ryu.topology.switches import Switches
from ryu.topology.switches import LLDPPacket
from ryu.app import simple_switch_13
import networkx as nx
import time
import setting

# import network_discovery

CONF = cfg.CONF


class DelayCollector(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    # _CONTEXTS = {"network_discovery": network_discovery.NetworkDiscovery}

    def __init__(self, *args, **kwargs):
        super(DelayCollector, self).__init__(*args, **kwargs)
        self.name = "delay"
        self.sending_echo_request_interval = 0.1 #rl
        # self.sending_echo_request_interval = 0.1 #drl
        # self.sw_module = get_switch(self, None)
        self.sw_module = lookup_service_brick('switches')
        self.discovery = lookup_service_brick('discovery')
        # self.discovery = kwargs['network_discovery']
        self.datapaths = {}
        self.echo_latency = {}
        self.link_delay = {}
        # Get initiation delay.
        self.initiation_delay = setting.DISCOVERY_PERIOD #wait topology discovery is done
        self.start_time = time.time()

        # Added by Hamed
        self.echo_sent_time = {}
        self.echo_rcv_time = {}

        self.measure_thread = hub.spawn_after(setting.MONITOR_AND_DELAYDETECTOR_BOOTSTRAP_DELAY,self._detector)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if not datapath.id in self.datapaths:
                self.logger.debug('Register datapath: %016x', datapath.id)
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                self.logger.debug('Unregister datapath: %016x', datapath.id)
                del self.datapaths[datapath.id]

    def _detector(self):
        """
            Delay detecting functon.
            This is the main function that periodically sends echo request and calculates link delay.
            Send echo request and calculate link delay periodically
            
        """
        while True:
            self._send_echo_request()
            self.create_link_delay()
            # try:
            #     self.discovery.shortest_paths = {}
            #     self.logger.debug("Refresh the shortest_paths")
            # except:
            #     self.discovery = lookup_service_brick('discovery')
            if self.discovery is not None:
                # self.get_link_delay()
                self.show_delay_statis()
            hub.sleep(setting.DELAY_DETECTING_PERIOD)# + (setting.MONITOR_PERIOD - MONITOR_PERIOD))


    def _send_echo_request(self):
        """
            Seng echo request msg to datapath.
        """
        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser
            data=("%.12f" % time.time()).encode()
            echo_req = parser.OFPEchoRequest(datapath,data)
            # echo_req = parser.OFPEchoRequest(datapath)
            
            #HAMED: instead of putting time within the packet I set the send time for each dpid
            self.echo_sent_time[datapath.id] = time.time()

            datapath.send_msg(echo_req)
            # Important! Don't send echo request together, Because it will
            # generate a lot of echo reply almost in the same time.
            # which will generate a lot of delay of waiting in queue
            # when processing echo reply in echo_reply_handler.

            hub.sleep(self.sending_echo_request_interval)

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def echo_reply_handler(self, ev):
        """
            Handle the echo reply msg, and get the latency of link.
        """
        now_timestamp = time.time()
        try:
            #Added by Hamed
            # self.echo_rcv_time[ev.msg.datapath.id] = time.time()

            latency = now_timestamp - eval(ev.msg.data.decode())
            
            #Modified by Hamed
            # latency =  self.echo_rcv_time[ev.msg.datapath.id] - self.echo_sent_time[ev.msg.datapath.id]
            
            self.echo_latency[ev.msg.datapath.id] = latency
        except:
            return

    def get_delay(self, src, dst):
        """
            Get link delay.
                        Controller
                        |        |
        src echo latency|        |dst echo latency
                        |        |
                   SwitchA-------SwitchB
                        
                    fwd_delay--->
                        <----reply_delay
            delay = (forward delay + reply delay - src datapath's echo latency
        """
        try:
            fwd_delay = self.discovery.graph[src][dst]['lldpdelay']
            re_delay = self.discovery.graph[dst][src]['lldpdelay']
            src_latency = self.echo_latency[src]
            dst_latency = self.echo_latency[dst]
            # print('link {0}-{1} --> ')
            delay = (fwd_delay + re_delay - src_latency - dst_latency)/2
            return max(delay, 0)
        except:
            return float(0)

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0):
        try:
            self.discovery.graph[src][dst]['lldpdelay'] = lldpdelay
        except:
            if self.discovery is None:
                self.discovery = lookup_service_brick('discovery')
            return

    def create_link_delay(self):
        """
            Create link delay data, and save it into graph object.
        """
        present_time = time.time()
        if present_time - self.start_time < self.initiation_delay: #Set to 30s
            return
        try:
            for src in self.discovery.graph:
                for dst in self.discovery.graph[src]:
                    if src == dst:
                        self.discovery.graph[src][dst]['delay'] = 0
                        continue
                    delay = self.get_delay(src, dst)
                    self.discovery.graph[src][dst]['delay'] = delay
            if self.discovery is not None:
                self.get_link_delay()
        except:
            if self.discovery is None:
                self.discovery = lookup_service_brick('discovery')
            return
           

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
            Explore LLDP packet and get the delay of link (fwd and reply).
        """
        msg = ev.msg
        try:
            src_dpid, src_port_no = LLDPPacket.lldp_parse(msg.data)
            dpid = msg.datapath.id
            if self.sw_module is None:
                self.sw_module = lookup_service_brick('switches')

            for port in self.sw_module.ports.keys():
                if src_dpid == port.dpid and src_port_no == port.port_no:
                    delay = self.sw_module.ports[port].delay
                    self._save_lldp_delay(src=src_dpid, dst=dpid,
                                          lldpdelay=delay)
        except LLDPPacket.LLDPUnknownFormat as e:
            return

    def get_link_delay(self):
        '''
        Calculates total link dealy and save it in self.link_delay[(node1,node2)]: link_delay
        '''
        i = time.time()
        for src in self.discovery.graph:
            for dst in self.discovery.graph[src]:
                if src != dst:
                    delay1 = self.discovery.graph[src][dst]['delay']
                    delay2 = self.discovery.graph[dst][src]['delay']
                    link_delay = ((delay1 + delay2)*1000.0)/2 #saves in ms
                    link = (src, dst)
                    self.link_delay[link] = link_delay
        # print(self.link_delay)
        # print('Time link_delay', time.time()-i)
        
    def show_delay_statis(self):
        if self.discovery is None:
            print("Not doing nothing, awarness none")
        else:
            print("Latency ok")
        if setting.TOSHOW and self.discovery is not None:
            self.logger.info("\nsrc   dst      delay")
            self.logger.info("---------------------------")
            for src in self.discovery.graph:
                for dst in self.discovery.graph[src]:
                    delay = self.discovery.graph[src][dst]['delay']
                    self.logger.info("%s   <-->   %s :   %s" % (src, dst, delay))

###########################################################################################
    # def send_port_stats_request(self, datapath):
    #     ofp = datapath.ofproto
    #     ofp_parser = datapath.ofproto_parser
    #     req = ofp_parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
    #     self.rtt_stats_sent[datapath.id] = time.time()
    #     datapath.send_msg(req)
    #     # save timeStamp for RTT

    # def send_flow_stats_request(self, datapath):
    #     ofp = datapath.ofproto
    #     ofp_parser = datapath.ofproto_parser
    #     # only the ones with layer 4
    #     match = ofp_parser.OFPMatch(eth_type=2048)
    #     req = ofp_parser.OFPFlowStatsRequest(datapath, 0, ofp.OFPTT_ALL,
    #                                          ofp.OFPP_ANY, ofp.OFPG_ANY, 0, 0, match)
    #     self.rtt_stats_sent[datapath.id] = time.time()
    #     datapath.send_msg(req)

    # @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    # def port_stats_reply_handler(self, ev):
    #     current_time = time.time()
    #     dpid_rec = ev.msg.datapath.id
    #     # updating switch controller latency
    #     old_time = self.rtt_stats_sent[dpid_rec]
    #     total_rtt = current_time - old_time
    #     self.rtt_portStats_to_dpid[dpid_rec] = total_rtt
    #     body = ev.msg.body
    #     # parsing the answer
    #     for statistic in body:
    #         # get port id
    #         port_no = int(statistic.port_no)
    #         # self.rtt_port_stats_sent[dpid_rec] = 0
    #         if dpid_rec in self.data_map.keys():
    #             for dpid_sent_element in self.data_map[dpid_rec]:
    #                 in_port = self.data_map[dpid_rec][dpid_sent_element]["in_port"]
    #                 if in_port == port_no:
    #                     # found the right connection
    #                     # check if bw-map is built, first time!
    #                     if dpid_rec not in self.temp_bw_map_ports.keys():
    #                         self.temp_bw_map_ports[dpid_rec] = {}
    #                         self.bandwith_port_dict[dpid_rec] = {}
    #                     if port_no not in self.temp_bw_map_ports[dpid_rec].keys():
    #                         self.temp_bw_map_ports[dpid_rec][port_no] = {}
    #                         bytes_now = statistic.rx_bytes
    #                         # bytes_now = stat.tx_bytes
    #                         ts_now = (statistic.duration_sec + statistic.duration_nsec / (10 ** 9))
    #                         # overwriting tempMap
    #                         self.temp_bw_map_ports[dpid_rec][port_no]['ts'] = ts_now
    #                         self.temp_bw_map_ports[dpid_rec][port_no]['bytes'] = bytes_now
    #                     else:
    #                         ts_before = self.temp_bw_map_ports[dpid_rec][port_no]['ts']
    #                         bytes_before = self.temp_bw_map_ports[dpid_rec][port_no]['bytes']
    #                         # ts_now = time.time()
    #                         bytes_now = statistic.tx_bytes
    #                         ts_now = (statistic.duration_sec + statistic.duration_nsec / (10 ** 9))
    #                         byte_diff = bytes_now - bytes_before
    #                         ts_diff = ts_now - ts_before
    #                         # overwriting tempMap
    #                         self.temp_bw_map_ports[dpid_rec][port_no]['ts'] = ts_now
    #                         self.temp_bw_map_ports[dpid_rec][port_no]['bytes'] = bytes_now
    #                         # bw (bytes/sec)
    #                         #Added By HAMED to fix the issue of zero ts_diff
    #                         if ts_diff:
    #                             bw = (byte_diff*8) / ts_diff
    #                         else:
    #                             bw = self.bandwith_port_dict[dpid_rec][port_no] 
    #                         self.bandwith_port_dict[dpid_rec][port_no] = bw


    #                         # Added by Hamed Jun 27, 2022 for producing dict src_sw:dst_sw:bw
    #                         mylinks=[(link.src.dpid,link.dst.dpid,link.src.port_no,link.dst.port_no) for link in get_link(self, None)]
    #                         for s,d,sp,dp in mylinks:
    #                             if s==dpid_rec and sp==port_no:
    #                                 self.bandwith_srcid_dstid_port_dict[s][d]=bw
    #                                 #Added by HAMED Jul, 27, 2022 to add per path plr (packet loss ratio)
    #                                 self.PortMap_send[s][sp]=statistic.tx_packets
    #                                 self.PortMap_recv[s][sp]=statistic.rx_packets

    #                                 #Added by HAMED Aug, 8, 2022 to add per link plr (packet loss ratio)
    #                                 self.s1_s2_send[s][d]=statistic.tx_packets
    #                                 self.s1_s2_recv[s][d]=statistic.rx_packets
    #                                 self.s1_s2_pdr[s][d]=self._get_link_pdr(self.s1_s2_send[s][d],self.s1_s2_recv[d][s])
    #                                 self.s1_s2_plr[s][d]=self._get_link_plr(self.s1_s2_send[s][d],self.s1_s2_recv[d][s])

                            
    #                         #Added by HAMED Jul, 27, 2022 to add plr (packet loss ratio)
    #                         for id_foward in self.chosen_path_per_flow.keys():
    #                             myPath=self.chosen_path_per_flow[id_foward]
    #                             h1,h2=self.plr_srcip_dstip_h1_h2[id_foward][0], self.plr_srcip_dstip_h1_h2[id_foward][1]
    #                             path_for_plr=self.add_ports_to_path(myPath,h1[1], h2[1])
    #                             self.plr_path_firstoutport_lastinport[tuple(myPath)]=[myPath[0],
    #                                 path_for_plr[myPath[0]][1], myPath[-1],path_for_plr[myPath[-1]][0]]
    #                             tx=self.PortMap_send[myPath[0]][path_for_plr[myPath[0]][1]]
    #                             rx=self.PortMap_recv[myPath[-1]][path_for_plr[myPath[-1]][0]]
    #                             self.path_plr[tuple(myPath)]=self._get_plr(tx,rx)
    #                             bw_cost=self.bandwith_srcid_dstid_port_dict[myPath[0]][myPath[1]]
    #                             for i in range(len(myPath) - 1):
    #                                 if self.bandwith_srcid_dstid_port_dict[myPath[i]][myPath[i+1]] < bw_cost :
    #                                     bw_cost = self.bandwith_srcid_dstid_port_dict[myPath[i]][myPath[i+1]]
    #                             self.path_bw[tuple(myPath)]=bw_cost


                           
    # # for getting flow stats
    # @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    # def flow_stats_reply_handler(self, ev):
    #     dpid_rec = ev.msg.datapath.id
    #     # updating switch controller latency
    #     self.rtt_portStats_to_dpid[dpid_rec] = time.time() - self.rtt_stats_sent[dpid_rec]

    #     for statistic in ev.msg.body:
    #         if 'icmpv4_type' not in statistic.match:
    #             ip_src = statistic.match['ipv4_src']
    #             ip_dst = statistic.match['ipv4_dst']
    #             number_bytes = statistic.byte_count
    #             if dpid_rec not in list(self.temp_bw_map_flows):
    #                 self.temp_bw_map_flows[dpid_rec] = {}
    #             if ip_src not in list(self.temp_bw_map_flows[dpid_rec]):
    #                 self.temp_bw_map_flows[dpid_rec][ip_src] = {}
    #             if ip_dst not in list(self.temp_bw_map_flows[dpid_rec][ip_src]):
    #                 self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst] = {}
    #                 ts_now = (statistic.duration_sec + statistic.duration_nsec / (10 ** 9))
    #                 self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['ts'] = ts_now
    #                 self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['bytes'] = statistic.byte_count
    #             # everything inside
    #             else:
    #                 ts_now = (statistic.duration_sec + statistic.duration_nsec / (10 ** 9))
    #                 time_diff = ts_now - self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['ts']
    #                 bytes_diff = number_bytes - self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['bytes']
    #                 if time_diff > 0.0:
    #                     try:
    #                         bw = (bytes_diff*8) / time_diff
    #                     except ZeroDivisionError:
    #                         self.logger.info(
    #                             "Saved_ts: {} ts_now: {} diff: {}".format(
    #                                 self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['ts'],
    #                                 ts_now, time_diff))
    #                     if dpid_rec not in list(self.bandwith_flow_dict.keys()):
    #                         self.bandwith_flow_dict[dpid_rec] = {}
    #                     if ip_src not in list(self.bandwith_flow_dict[dpid_rec].keys()):
    #                         self.bandwith_flow_dict[dpid_rec][ip_src] = {}
    #                     self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['ts'] = ts_now
    #                     self.temp_bw_map_flows[dpid_rec][ip_src][ip_dst]['bytes'] = statistic.byte_count
    #                     self.bandwith_flow_dict[dpid_rec][ip_src][ip_dst] = bw
