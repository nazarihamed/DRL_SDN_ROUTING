## DRL_SDN_ROUTING

First run Ryu controller using following command:

	ryu-manager --observe-links network_statistics.py

 - Just consider all the links capacities 100mbps for testing purpose, then it should read the links capacities from **bw_r.txt** file. TODO so, just need to search for bw_r.txt within **network_statistics.py** file.


Then, quickly run topology, located on topo directory, in mininet using following command:

	sudo python3 DRSIR_new32.py


List of files and dirs:
- **network_statistics.py** is the main monitoring module. It creates the **net_info.csv** and **paths_metrics.json** files to feed into the DRL module.
- **delay_collector.py** is the module for calculating delay. Since delay measurement is a bit tricky and different from other statistics in ryu. We provided another module for calculating delay.
- **network_discovery.py** is the module for discovering the topology. At the begining controller waits to all the topology be discovered. Then, it starts monitoring process.
- **topo** directory contains topology and mininet scripts
- **miscNotebook.ipynb** notebook is only for testing and logging purposes 
 

 **TODO:** flow_info = (ip_src, ip_dst) in flow_install_monitor() -> forwarding() -> install_flow()  (in network_statistics.py) need to be resolved to be able to add flow entries to switches. ether_type and ethernet src and dst need to be added to flow_info
