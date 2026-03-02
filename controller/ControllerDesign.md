# Controller Design

The StreamBed controller is used to synchronize and deploy models across edge and server devices. It handles versioning and deployment of model and embedding versions. \
\
It also works with a network of distributed containers in a sharding fashion. Each controller is responsible for heartbeat monitoring of the status of its associated devices. There is also an autoscaling feature to handle increased device load. \
\
The controller also provides a centralized reliability dashboard for device status and connectivity. The user manages these processes through API calls, routed to the correct controller through an reverse proxy router.

## Model Versioning and Deployment

The models themselves will be hosted on an external service (HuggingFace), which the user can push to on a branch.\
\
A separate PUT request is used to update a list of specified devices with the docker container specified by the request body. The request body in this case will be the repo+commit hash+filepath. If the docker container hasn't been generated yet, the deployment system automatically pulls the commit from huggingface, build a standardized docker container, and push to dockerhub, labelled by a unique hash built from the repo+commit hash+filepath.\
\
The request will then be shard routed to the controller workers, which each contain a map of `device_cluster`/`device_id` to IP. The controller workers then send deployment requests to the corresponding devices, which each have a locally running deployment daemon. When sent a request, the daemon pulls in a docker container from dockerhub, launches it, and reroutes data input/output through this container. Once everything looks good to go, the daemon then deletes the old container.

## Container Sharding
The system is designed to scale to unknown number of servers/devices. Each `device_cluster` represents a grouping of servers and edge devices designed to work together. The `device_cluster` is hashed, and requests are sent to the corresponding controller based on the hash. \
\
The routing is handled locally at the request router. The router contains a self-balancing binary tree, mapping hash space to specific nodes. If the count on specific controller node is too high, a new node is added to the tree, which self-balances. The tree is associated with a cache as well, so it's mostly O(1) retrieval with O(logn) on cache miss.

## Monitoring
One of the primary functions of the controller system is to allow for easy monitoring of the system. \
\
The user can filter by device cluster, showing data from the corresponding StreamBed controller. This will show the internal routing of data, and the status of each device. This will also show the currently running model on each device. **It is on the user to verify model compatibility and proper dataflow.** \
\
Each controller is responsible for the `device_cluster/device_id`s mapped to it. It has an API listening for a heartbeat, which contains the current model running, and the status. These heartbeats update a status table keyed by `device_cluster/device_id`. These heartbeats are retrieved when the user wants a monitor update on the system.

## Adding/Removing Devices
The user can register new devices to a new/preexisting cluster through an API. This request is routed to the appropriate controller, which adds the IP to the table. The device itself should have the daemon only allowing traffic from the controller's IP. \
A similar process will allow for shutdown. A shutdown request will be sent to the deployment daemon. After a valid response, the device will be removed from the table.

## Rerouting
The routing from edge device to server is configured by the user. The deployment daemon running on edge is responsible for sending to IPs. A reroute request can be sent to reroute traffic from a specified `device_cluster/device_id` to another `device_cluster/device_id`.\
\
Because the system's usecase is limited to the StreamBed architecture, this rerouting can be triggered manually. A table at the controller level stores the routing, allowing for persistent state.