"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""
import os
import random
from collections import defaultdict
from collections import deque
from contextlib import suppress
from logging import Logger
from tempfile import TemporaryDirectory
from typing import Set, Tuple

import OpenSSL
import maya
import requests
import time
from constant_sorrow import constants
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import Certificate, NameOID
from eth_keys.datatypes import Signature as EthSignature
from requests.exceptions import SSLError
from twisted.internet import reactor, defer
from twisted.internet import task
from twisted.internet.threads import deferToThread
from twisted.logger import Logger

from nucypher.config.constants import SeednodeMetadata
from nucypher.config.keyring import _write_tls_certificate
from nucypher.config.storages import InMemoryNodeStorage
from nucypher.crypto.api import keccak_digest
from nucypher.crypto.powers import BlockchainPower, SigningPower, EncryptingPower, NoSigningPower
from nucypher.crypto.signing import signature_splitter
from nucypher.network.middleware import RestMiddleware
from nucypher.network.nicknames import nickname_from_seed
from nucypher.network.protocols import SuspiciousActivity
from nucypher.network.server import TLSHostingPower

import sys
import json
import struct
import datetime

def get_sysdate():  
    now = datetime.datetime.now()
    return str(now)
    
def encodeMessage(messageContent):
    encodedContent = json.dumps(messageContent).encode('utf-8')
    encodedLength = struct.pack('@I', len(encodedContent))
    return {'length': encodedLength, 'content': encodedContent}

# Send an encoded message to stdout
def sendMessage(encodedMessage):
    sys.stdout.buffer.write(encodedMessage['length'])
    sys.stdout.buffer.write(encodedMessage['content'])
    sys.stdout.buffer.flush()

class FleetState(dict):
    """
    A representation of a fleet of NuCypher nodes.
    """
    _checksum = constants.NO_KNOWN_NODES.bool_value(False)
    _nickname = constants.NO_KNOWN_NODES
    _nickname_metadata = constants.NO_KNOWN_NODES
    most_recent_node_change = constants.NO_KNOWN_NODES

    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.updated = maya.now()

    @property
    def checksum(self):
        return self._checksum

    @checksum.setter
    def checksum(self, checksum_value):
        self._checksum = checksum_value
        self._nickname, self._nickname_metadata = nickname_from_seed(checksum_value, number_of_pairs=1)

    @property
    def nickname(self):
        return self._nickname

    @property
    def nickname_metadata(self):
        return self._nickname_metadata

    def icon(self):
        if self.checksum is constants.NO_KNOWN_NODES:
            return "NO FLEET STATE AVAILABLE"
        icon_template = """
        <div class="nucypher-nickname-icon" style="border-color:{color};">
        <span class="symbol" style="color: {color}">{symbol}</span>
        <br/>
        <span class="small-address">{fleet_state_checksum}</span>
        </div>
        """.replace("  ", "").replace('\n', "")
        return icon_template.format(
            color=self.nickname_metadata[0][0]['hex'],
            symbol=self.nickname_metadata[0][1],
            fleet_state_checksum=self.checksum[0:8]
        )


class Learner:
    """
    Any participant in the "learning loop" - a class inheriting from
    this one has the ability, synchronously or asynchronously,
    to learn about nodes in the network, verify some essential
    details about them, and store information about them for later use.
    """

    _SHORT_LEARNING_DELAY = 5
    _LONG_LEARNING_DELAY = 90
    LEARNING_TIMEOUT = 10
    _ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN = 10

    # For Keeps
    __DEFAULT_NODE_STORAGE = InMemoryNodeStorage
    __DEFAULT_MIDDLEWARE_CLASS = RestMiddleware

    class NotEnoughTeachers(RuntimeError):
        pass

    class UnresponsiveTeacher(ConnectionError):
        pass

    def __init__(self,
                 network_middleware: RestMiddleware = __DEFAULT_MIDDLEWARE_CLASS(),
                 start_learning_now: bool = False,
                 learn_on_same_thread: bool = False,
                 known_nodes: tuple = None,
                 seed_nodes: Tuple[tuple] = None,
                 known_certificates_dir: str = None,
                 node_storage=None,
                 save_metadata: bool = False,
                 abort_on_learning_error: bool = False
                 ) -> None:

        self.log = Logger("characters")  # type: Logger
        self.network_middleware = network_middleware
        self.save_metadata = save_metadata
        self.start_learning_now = start_learning_now
        self.learn_on_same_thread = learn_on_same_thread

        self._abort_on_learning_error = abort_on_learning_error
        self._learning_listeners = defaultdict(list)
        self._node_ids_to_learn_about_immediately = set()

        self.known_certificates_dir = known_certificates_dir or TemporaryDirectory("nucypher-tmp-certs-").name
        self.__known_nodes = FleetState()

        self.done_seeding = False

        # Read
        if node_storage is None:
            node_storage = self.__DEFAULT_NODE_STORAGE(federated_only=self.federated_only,
                                                       # TODO: remove federated_only
                                                       character_class=self.__class__)

        self.node_storage = node_storage
        if save_metadata and node_storage is constants.NO_STORAGE_AVAILIBLE:
            raise ValueError("Cannot save nodes without a configured node storage")

        known_nodes = known_nodes or tuple()
        self.unresponsive_startup_nodes = list()  # TODO: Attempt to use these again later
        for node in known_nodes:
            try:
                self.remember_node(node, update_fleet_state=False)  # TODO: Need to test this better - do we ever init an Ursula-Learner with Node Storage?
            except self.UnresponsiveTeacher:
                self.unresponsive_startup_nodes.append(node)

        self.teacher_nodes = deque()
        self._current_teacher_node = None  # type: Teacher
        self._learning_task = task.LoopingCall(self.keep_learning_about_nodes)
        self._learning_round = 0  # type: int
        self._rounds_without_new_nodes = 0  # type: int
        self._seed_nodes = seed_nodes or []
        self.unresponsive_seed_nodes = set()

        if self.start_learning_now:
            self.start_learning_loop(now=self.learn_on_same_thread)

    @property
    def known_nodes(self):
        return self.__known_nodes

    def load_seednodes(self,
                       read_storages: bool = True,
                       retry_attempts: int = 3,
                       retry_rate: int = 2,
                       timeout=3):
        """
        Engage known nodes from storages and pre-fetch hardcoded seednode certificates for node learning.
        """
        if self.done_seeding:
            sendMessage(encodeMessage("log:Level:Debug, Date:{}, Message:Already done seeding; won't try again.".format(get_sysdate())))
            #self.log.debug("Already done seeding; won't try again.")
            return

        def __attempt_seednode_learning(seednode_metadata, current_attempt=1):
            from nucypher.characters.lawful import Ursula
            sendMessage(encodeMessage("log:Level:Debug, Date:{}, Message:Seeding from: {}|{}:{}".format(get_sysdate(), seednode_metadata.checksum_address,seednode_metadata.rest_host,seednode_metadata.rest_port)))
            #self.log.debug(
            #    "Seeding from: {}|{}:{}".format(seednode_metadata.checksum_address,
             #                                   seednode_metadata.rest_host,
             #                                   seednode_metadata.rest_port))

            seed_node = Ursula.from_seednode_metadata(seednode_metadata=seednode_metadata,
                                                      network_middleware=self.network_middleware,
                                                      certificates_directory=self.known_certificates_dir,
                                                      timeout=timeout,
                                                      federated_only=self.federated_only)  # TODO: 466
            if seed_node is False:
                self.unresponsive_seed_nodes.add(seednode_metadata)
            else:
                self.unresponsive_seed_nodes.discard(seednode_metadata)
                self.remember_node(seed_node)

        for seednode_metadata in self._seed_nodes:
            __attempt_seednode_learning(seednode_metadata=seednode_metadata)

        if not self.unresponsive_seed_nodes:
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Finished learning about all seednodes.".format(get_sysdate())))
            #self.log.info("Finished learning about all seednodes.")
        self.done_seeding = True

        if read_storages is True:
            self.read_nodes_from_storage()

        if not self.known_nodes:
            sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: No seednodes were available after {} attempts".format(get_sysdate(), retry_attempts)))
            #self.log.warn("No seednodes were available after {} attempts".format(retry_attempts))
            # TODO: Need some actual logic here for situation with no seed nodes (ie, maybe try again much later)

    def read_nodes_from_storage(self) -> set:
        stored_nodes = self.node_storage.all(federated_only=self.federated_only)  # TODO: 466
        for node in stored_nodes:
            self.remember_node(node)

    def sorted_nodes(self):
        nodes_to_consider = list(self.known_nodes.values())
        return sorted(nodes_to_consider, key=lambda n: n.checksum_public_address)

    def remember_node(self, node, force_verification_check=False, update_fleet_state=True):

        if node == self:  # No need to remember self.
            return False

        # First, determine if this is an outdated representation of an already known node.
        with suppress(KeyError):
            already_known_node = self.known_nodes[node.checksum_public_address]
            if not node.timestamp > already_known_node.timestamp:   
                sendMessage(encodeMessage("log:Level:Debug, Date:{}, Message: Skipping already known node {}".format(get_sysdate(), already_known_node )))          
                #self.log.debug("Skipping already known node {}".format(already_known_node))
                # This node is already known.  We can safely return.
                return False

        node.save_certificate_to_disk(directory=self.known_certificates_dir, force=True)  # TODO: Verify before force?
        certificate_filepath = node.get_certificate_filepath(certificates_dir=self.known_certificates_dir)
        try:
            node.verify_node(force=force_verification_check,
                             network_middleware=self.network_middleware,
                             accept_federated_only=self.federated_only,  # TODO: 466
                             certificate_filepath=certificate_filepath)
        except SSLError:
            return False  # TODO: Bucket this node as having bad TLS info - maybe it's an update that hasn't fully propagated?
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Skipping already known node {}".format(get_sysdate(), already_known_node )))    
            #self.log.info("No Response while trying to verify node {}|{}".format(node.rest_interface, node))
            return False  # TODO: Bucket this node as "ghost" or something: somebody else knows about it, but we can't get to it.

        listeners = self._learning_listeners.pop(node.checksum_public_address, tuple())
        address = node.checksum_public_address

        self.__known_nodes[address] = node

        if self.save_metadata:
            self.write_node_metadata(node=node)

        sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Remembering {}, popping {} listeners.".format(get_sysdate(), node.checksum_public_address, len(listeners) )))  
        #self.log.info("Remembering {}, popping {} listeners.".format(node.checksum_public_address, len(listeners)))
        for listener in listeners:
            listener.add(address)
        self._node_ids_to_learn_about_immediately.discard(address)

        if update_fleet_state:
            self.update_fleet_stfate()

        return True

    def update_fleet_state(self):
        # TODO: Probably not mutate these foreign attrs - ideally maybe move quite a bit of this method up to FleetState (maybe in __setitem__).
        self.known_nodes.checksum = keccak_digest(b"".join(bytes(n) for n in self.sorted_nodes())).hex()
        self.known_nodes.updated = maya.now()

    def start_learning_loop(self, now=False):
        if self._learning_task.running:
            return False
        elif now:
            self.load_seednodes()
            self._learning_task()  # Unhandled error might happen here.  TODO: Call this in a safer place.
            self.learning_deferred = self._learning_task.start(interval=self._SHORT_LEARNING_DELAY)
            self.learning_deferred.addErrback(self.handle_learning_errors)
            return self.learning_deferred
        else:
            seeder_deferred = deferToThread(self.load_seednodes)
            learner_deferred = self._learning_task.start(interval=self._SHORT_LEARNING_DELAY, now=now)
            seeder_deferred.addErrback(self.handle_learning_errors)
            learner_deferred.addErrback(self.handle_learning_errors)
            self.learning_deferred = defer.DeferredList([seeder_deferred, learner_deferred])
            return self.learning_deferred

    def stop_learning_loop(self):
        """
        Only for tests at this point.  Maybe some day for graceful shutdowns.
        """

    def handle_learning_errors(self, *args, **kwargs):
        failure = args[0]
        if self._abort_on_learning_error:
            sendMessage(encodeMessage("log:Level:Critical, Date:{}, Message: Unhandled error during node learning.  Attempting graceful crash.".format(get_sysdate())))   
            #self.log.critical("Unhandled error during node learning.  Attempting graceful crash.")
            reactor.callFromThread(self._crash_gracefully, failure=failure)
        else:
            sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Unhandled error during node learning: {}".format(get_sysdate(), failure.getTraceback())))   
            #self.log.warn("Unhandled error during node learning: {}".format(failure.getTraceback()))
            if not self._learning_task.running:
                self.start_learning_loop()  # TODO: Consider a single entry point for this with more elegant pause and unpause.

    def _crash_gracefully(self, failure=None):
        """
        A facility for crashing more gracefully in the event that an exception
        is unhandled in a different thread, especially inside a loop like the learning loop.
        """
        self._crashed = failure
        failure.raiseException()
        # TODO: We don't actually have checksum_public_address at this level - maybe only Characters can crash gracefully :-)
        sendMessage(encodeMessage("log:Level:Critical, Date:{}, Message: {} crashed with {}".format(get_sysdate(), self.checksum_public_address, failure)))  
        #self.log.critical("{} crashed with {}".format(self.checksum_public_address, failure))

    def shuffled_known_nodes(self):
        nodes_we_know_about = list(self.__known_nodes.values())
        random.shuffle(nodes_we_know_about)
        sendMessage(encodeMessage("knownnodes:Date:{}, Message:{}known nodes".format(get_sysdate(), len(nodes_we_know_about))))  
        sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Shuffled {} known nodes".format(get_sysdate(), len(nodes_we_know_about))))  
        #self.log.info("Shuffled {} known nodes".format(len(nodes_we_know_about)))
        return nodes_we_know_about

    def select_teacher_nodes(self):
        nodes_we_know_about = self.shuffled_known_nodes()

        if not nodes_we_know_about:
            raise self.NotEnoughTeachers("Need some nodes to start learning from.")

        self.teacher_nodes.extend(nodes_we_know_about)

    def cycle_teacher_node(self):
        # To ensure that all the best teachers are availalble, first let's make sure
        # that we have connected to all the seed nodes.
        if self.unresponsive_seed_nodes:
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Still have unresponsive seed nodes; trying again to connect.".format(get_sysdate())))
            #self.log.info("Still have unresponsive seed nodes; trying again to connect.")
            self.load_seednodes()  # Ideally, this is async and singular.

        if not self.teacher_nodes:
            self.select_teacher_nodes()
        try:
            self._current_teacher_node = self.teacher_nodes.pop()
        except IndexError:
            error = "Not enough nodes to select a good teacher, Check your network connection then node configuration"
            raise self.NotEnoughTeachers(error)
        sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Cycled teachers; New teacher is {}".format(get_sysdate(), self._current_teacher_node.checksum_public_address)))
        #self.log.info("Cycled teachers; New teacher is {}".format(self._current_teacher_node.checksum_public_address))

    def current_teacher_node(self, cycle=False):
        if cycle:
            self.cycle_teacher_node()

        if not self._current_teacher_node:
            self.cycle_teacher_node()

        teacher = self._current_teacher_node

        return teacher

    def learn_about_nodes_now(self, force=False):
        if self._learning_task.running:
            self._learning_task.reset()
            self._learning_task()
        elif not force:
            sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Learning loop isn't started; can't learn about nodes now.  You can override this with force=True.".format(get_sysdate())))
            #self.log.warn(
            #    "Learning loop isn't started; can't learn about nodes now.  You can override this with force=True.")
        elif force:
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Learning loop wasn't started; forcing start now.".format(get_sysdate())))
            #self.log.info("Learning loop wasn't started; forcing start now.")
            self._learning_task.start(self._SHORT_LEARNING_DELAY, now=True)

    def keep_learning_about_nodes(self):
        """
        Continually learn about new nodes.
        """
        self.learn_from_teacher_node(eager=False)  # TODO: Allow the user to set eagerness?

    def learn_about_specific_nodes(self, canonical_addresses: Set):
        self._node_ids_to_learn_about_immediately.update(canonical_addresses)  # hmmmm
        self.learn_about_nodes_now()

    # TODO: Dehydrate these next two methods.

    def block_until_number_of_known_nodes_is(self,
                                             number_of_nodes_to_know: int,
                                             timeout: int = 10,
                                             learn_on_this_thread: bool = False):
        start = maya.now()
        starting_round = self._learning_round

        while True:
            rounds_undertaken = self._learning_round - starting_round
            if len(self.__known_nodes) >= number_of_nodes_to_know:
                if rounds_undertaken:
                   sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Learned about enough nodes after {} rounds.".format(get_sysdate(), rounds_undertaken)))
                   # self.log.info("Learned about enough nodes after {} rounds.".format(rounds_undertaken))
                return True

            if not self._learning_task.running:
                sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Blocking to learn about nodes, but learning loop isn't running.".format(get_sysdate())))
                #self.log.warn("Blocking to learn about nodes, but learning loop isn't running.")
            if learn_on_this_thread:
                try:
                    self.learn_from_teacher_node(eager=True)
                except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout):
                    sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Teacher was unreachable.  No good way to handle this on the main thread.".format(get_sysdate())))
                    # TODO: Even this "same thread" logic can be done off the main thread.
                    #self.log.warn("Teacher was unreachable.  No good way to handle this on the main thread.")

            # The rest of the fucking owl
            if (maya.now() - start).seconds > timeout:
                if not self._learning_task.running:
                    raise self.NotEnoughTeachers("Learning loop is not running.  Start it with start_learning().")
                else:
                    raise self.NotEnoughTeachers("After {} seconds and {} rounds, didn't find {} nodes".format(
                        timeout, rounds_undertaken, number_of_nodes_to_know))
            else:
                time.sleep(.1)

    def block_until_specific_nodes_are_known(self,
                                             canonical_addresses: Set,
                                             timeout=LEARNING_TIMEOUT,
                                             allow_missing=0,
                                             learn_on_this_thread=False):
        start = maya.now()
        starting_round = self._learning_round

        while True:
            if self._crashed:
                return self._crashed
            rounds_undertaken = self._learning_round - starting_round
            if canonical_addresses.issubset(self.__known_nodes):
                if rounds_undertaken:
                    sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Learned about all nodes after {} rounds.".format(get_sysdate(), rounds_undertaken)))
                    #self.log.info("Learned about all nodes after {} rounds.".format(rounds_undertaken))
                return True

            if not self._learning_task.running:
                sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Blocking to learn about nodes, but learning loop isn't running.".format(get_sysdate())))
                #self.log.warn("Blocking to learn about nodes, but learning loop isn't running.")
            if learn_on_this_thread:
                self.learn_from_teacher_node(eager=True)

            if (maya.now() - start).seconds > timeout:

                still_unknown = canonical_addresses.difference(self.__known_nodes)

                if len(still_unknown) <= allow_missing:
                    return False
                elif not self._learning_task.running:
                    raise self.NotEnoughTeachers("The learning loop is not running.  Start it with start_learning().")
                else:
                    raise self.NotEnoughTeachers(
                        "After {} seconds and {} rounds, didn't find these {} nodes: {}".format(
                            timeout, rounds_undertaken, len(still_unknown), still_unknown))

            else:
                time.sleep(.1)

    def _adjust_learning(self, node_list):
        """
        Takes a list of new nodes, adjusts learning accordingly.

        Currently, simply slows down learning loop when no new nodes have been discovered in a while.
        TODO: Do other important things - scrub, bucket, etc.
        """
        if node_list:
            self._rounds_without_new_nodes = 0
            self._learning_task.interval = self._SHORT_LEARNING_DELAY
        else:
            self._rounds_without_new_nodes += 1
            if self._rounds_without_new_nodes > self._ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN:
                sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: After {} rounds with no new nodes, it's time to slow down to {} seconds.".format(get_sysdate(), self._ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN, self._LONG_LEARNING_DELAY)))
                #self.log.info("After {} rounds with no new nodes, it's time to slow down to {} seconds.".format(
                #    self._ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN,
                #    self._LONG_LEARNING_DELAY))
                self._learning_task.interval = self._LONG_LEARNING_DELAY

    def _push_certain_newly_discovered_nodes_here(self, queue_to_push, node_addresses):
        """
        If any node_addresses are discovered, push them to queue_to_push.
        """
        for node_address in node_addresses:
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Adding listener for {}".format(get_sysdate(), node_address)))
            #self.log.info("Adding listener for {}".format(node_address))
            self._learning_listeners[node_address].append(queue_to_push)

    def network_bootstrap(self, node_list: list) -> None:
        for node_addr, port in node_list:
            new_nodes = self.learn_about_nodes_now(node_addr, port)
            self.__known_nodes.update(new_nodes)

    def get_nodes_by_ids(self, node_ids):
        for node_id in node_ids:
            try:
                # Scenario 1: We already know about this node.
                return self.__known_nodes[node_id]
            except KeyError:
                raise NotImplementedError
        # Scenario 2: We don't know about this node, but a nearby node does.
        # TODO: Build a concurrent pool of lookups here.

        # Scenario 3: We don't know about this node, and neither does our friend.

    def write_node_metadata(self, node, serializer=bytes) -> str:
        return self.node_storage.save(node=node)

    def learn_from_teacher_node(self, eager=True):
        """
        Sends a request to node_url to find out about known nodes.
        """
        self._learning_round += 1

        try:
            current_teacher = self.current_teacher_node()
        except self.NotEnoughTeachers as e:
            sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Can't learn right now: {}".format(get_sysdate(), e.args[0])))
            #self.log.warn("Can't learn right now: {}".format(e.args[0]))
            return

        rest_url = current_teacher.rest_interface  # TODO: Name this..?

        # TODO: Do we really want to try to learn about all these nodes instantly?
        # Hearing this traffic might give insight to an attacker.
        if VerifiableNode in self.__class__.__bases__:
            announce_nodes = [self]
        else:
            announce_nodes = None

        unresponsive_nodes = set()
        try:

            # TODO: Streamline path generation
            certificate_filepath = current_teacher.get_certificate_filepath(
                certificates_dir=self.known_certificates_dir)
            response = self.network_middleware.get_nodes_via_rest(url=rest_url,
                                                                  nodes_i_need=self._node_ids_to_learn_about_immediately,
                                                                  announce_nodes=announce_nodes,
                                                                  certificate_filepath=certificate_filepath)
        except requests.exceptions.ConnectionError as e:
            unresponsive_nodes.add(current_teacher)
            teacher_rest_info = current_teacher.rest_information()[0]

            # TODO: This error isn't necessarily "no repsonse" - let's maybe pass on the text of the exception here.
            sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: No Response from teacher: {}:{}.".format(get_sysdate(), teacher_rest_info.host, teacher_rest_info.port )))
            #self.log.info("No Response from teacher: {}:{}.".format(teacher_rest_info.host, teacher_rest_info.port))
            self.cycle_teacher_node()
            return

        if response.status_code != 200:
            raise RuntimeError("Bad response from teacher: {} - {}".format(response, response.content))

        signature, nodes = signature_splitter(response.content, return_remainder=True)

        # TODO: This doesn't make sense - a decentralized node can still learn about a federated-only node.
        from nucypher.characters.lawful import Ursula
        node_list = Ursula.batch_from_bytes(nodes, federated_only=self.federated_only)  # TODO: 466

        new_nodes = []
        for node in node_list:
            try:
                if eager:
                    certificate_filepath = current_teacher.get_certificate_filepath(
                        certificates_dir=self.known_certificates_dir)
                    node.verify_node(self.network_middleware,
                                     accept_federated_only=self.federated_only,  # TODO: 466
                                     certificate_filepath=certificate_filepath)
                    sendMessage(encodeMessage("log:Level:Debug, Date:{}, Message: Verified node: {}".format(get_sysdate(), node.checksum_public_address)))
                    #self.log.debug("Verified node: {}".format(node.checksum_public_address))

                else:
                    node.validate_metadata(accept_federated_only=self.federated_only)  # TODO: 466

            except node.SuspiciousActivity:
                # TODO: Account for possibility that stamp, rather than interface, was bad.
                message = "Suspicious Activity: Discovered node with bad signature: {}.  " \
                          "Propagated by: {}".format(current_teacher.checksum_public_address, rest_url)
                sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: {}".format(get_sysdate(), message)))
                #self.log.warn(message)
            new = self.remember_node(node)
            if new:
                new_nodes.append(node)

        self._adjust_learning(new_nodes)

        learning_round_log_message = "Learning round {}.  Teacher: {} knew about {} nodes, {} were new."
        current_teacher.last_seen = maya.now()
        self.cycle_teacher_node()
        sendMessage(encodeMessage("log:Level:Info, Date:{}, Message: Learning round {}.  Teacher: {} knew about {} nodes, {} were new.".format(get_sysdate(), self._learning_round, current_teacher, len(node_list), len(new_nodes) )))
        #self.log.info(learning_round_log_message.format(self._learning_round,
        #                                                current_teacher,
         #                                               len(node_list),
         #                                               len(new_nodes)), )
        if new_nodes and self.known_certificates_dir:
            for node in new_nodes:
                node.save_certificate_to_disk(self.known_certificates_dir, force=True)

        return new_nodes


class VerifiableNode:
    _evidence_of_decentralized_identity = constants.NOT_SIGNED
    verified_stamp = False
    verified_interface = False
    _verified_node = False
    _interface_info_splitter = (int, 4, {'byteorder': 'big'})
    log = Logger("network/nodes")

    def __init__(self,
                 certificate: Certificate,
                 certificate_filepath: str,
                 interface_signature=constants.NOT_SIGNED.bool_value(False),
                 timestamp=constants.NOT_SIGNED,
                 ) -> None:

        self.certificate = certificate
        self.certificate_filepath = certificate_filepath
        self._interface_signature_object = interface_signature
        self._timestamp = timestamp
        self.last_seen = constants.NEVER_SEEN("Haven't connected to this node yet.")

    class InvalidNode(SuspiciousActivity):
        """
        Raised when a node has an invalid characteristic - stamp, interface, or address.
        """

    class WrongMode(TypeError):
        """
        Raise when a Character tries to use another Character as decentralized when the latter is federated_only.
        """

    def seed_node_metadata(self):
        return SeednodeMetadata(self.checksum_public_address,
                                self.rest_server.rest_interface.host,
                                self.rest_server.rest_interface.port)

    @classmethod
    def from_tls_hosting_power(cls, tls_hosting_power: TLSHostingPower, *args, **kwargs) -> 'VerifiableNode':
        certificate_filepath = tls_hosting_power.keypair.certificate_filepath
        certificate = tls_hosting_power.keypair.certificate
        return cls(certificate=certificate, certificate_filepath=certificate_filepath, *args, **kwargs)

    def sorted_nodes(self):
        nodes_to_consider = list(self.known_nodes.values()) + [self]
        return sorted(nodes_to_consider, key=lambda n: n.checksum_public_address)

    def _stamp_has_valid_wallet_signature(self):
        signature_bytes = self._evidence_of_decentralized_identity
        if signature_bytes is constants.NOT_SIGNED:
            return False
        else:
            signature = EthSignature(signature_bytes)
        proper_pubkey = signature.recover_public_key_from_msg(bytes(self.stamp))
        proper_address = proper_pubkey.to_checksum_address()
        return proper_address == self.checksum_public_address

    def stamp_is_valid(self):
        """
        :return:
        """
        signature = self._evidence_of_decentralized_identity
        if self._stamp_has_valid_wallet_signature():
            self.verified_stamp = True
            return True
        elif self.federated_only and signature is constants.NOT_SIGNED:
            message = "This node can't be verified in this manner, " \
                      "but is OK to use in federated mode if you" \
                      " have reason to believe it is trustworthy."
            raise self.WrongMode(message)
        else:
            raise self.InvalidNode

    def interface_is_valid(self):
        """
        Checks that the interface info is valid for this node's canonical address.
        """
        interface_info_message = self._signable_interface_info_message()  # Contains canonical address.
        message = self.timestamp_bytes() + interface_info_message
        interface_is_valid = self._interface_signature.verify(message, self.public_keys(SigningPower))
        self.verified_interface = interface_is_valid
        if interface_is_valid:
            return True
        else:
            raise self.InvalidNode

    def verify_id(self, ursula_id, digest_factory=bytes):
        self.verify()
        if not ursula_id == digest_factory(self.canonical_public_address):
            raise self.InvalidNode

    def validate_metadata(self, accept_federated_only=False):
        if not self.verified_interface:
            self.interface_is_valid()
        if not self.verified_stamp:
            try:
                self.stamp_is_valid()
            except self.WrongMode:
                if not accept_federated_only:
                    raise

    def verify_node(self,
                    network_middleware,
                    certificate_filepath: str = None,
                    accept_federated_only: bool = False,
                    force: bool = False
                    ) -> bool:
        """
        Three things happening here:

        * Verify that the stamp matches the address (raises InvalidNode is it's not valid, or WrongMode if it's a federated mode and being verified as a decentralized node)
        * Verify the interface signature (raises InvalidNode if not valid)
        * Connect to the node, make sure that it's up, and that the signature and address we checked are the same ones this node is using now. (raises InvalidNode if not valid; also emits a specific warning depending on which check failed).
        """
        if not force:
            if self._verified_node:
                return True

        self.validate_metadata(accept_federated_only)  # This is both the stamp and interface check.

        # The node's metadata is valid; let's be sure the interface is in order.
        response = network_middleware.node_information(host=self.rest_information()[0].host,
                                                       port=self.rest_information()[0].port,
                                                       certificate_filepath=certificate_filepath)

        if not response.status_code == 200:
            raise RuntimeError("Or something.")  # TODO: Raise an error here?  Or return False?  Or something?
        timestamp, signature, identity_evidence, \
        verifying_key, encrypting_key, \
        public_address, certificate_vbytes, rest_info = self._internal_splitter(response.content)

        verifying_keys_match = verifying_key == self.public_keys(SigningPower)
        encrypting_keys_match = encrypting_key == self.public_keys(EncryptingPower)
        addresses_match = public_address == self.canonical_public_address
        evidence_matches = identity_evidence == self._evidence_of_decentralized_identity

        if not all((encrypting_keys_match, verifying_keys_match, addresses_match, evidence_matches)):
            # TODO: Optional reporting.  355
            if not addresses_match:
               sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Wallet address swapped out.  It appears that someone is trying to defraud this node.".format(get_sysdate())))
               # self.log.warn("Wallet address swapped out.  It appears that someone is trying to defraud this node.")
            if not verifying_keys_match:
                sendMessage(encodeMessage("log:Level:Warn, Date:{}, Message: Verifying key swapped out.  It appears that someone is impersonating this node.".format(get_sysdate())))
                #self.log.warn("Verifying key swapped out.  It appears that someone is impersonating this node.")
            raise self.InvalidNode("Wrong cryptographic material for this node - something fishy going on.")
        else:
            self._verified_node = True

    def substantiate_stamp(self, passphrase: str):
        blockchain_power = self._crypto_power.power_ups(BlockchainPower)
        blockchain_power.unlock_account(password=passphrase)  # TODO: 349
        signature = blockchain_power.sign_message(bytes(self.stamp))
        self._evidence_of_decentralized_identity = signature

    def _signable_interface_info_message(self):
        message = self.canonical_public_address + self.rest_information()[0]
        return message

    def _sign_and_date_interface_info(self):
        message = self._signable_interface_info_message()
        self._timestamp = maya.now()
        self._interface_signature_object = self.stamp(self.timestamp_bytes() + message)

    @property
    def _interface_signature(self):
        if not self._interface_signature_object:
            try:
                self._sign_and_date_interface_info()
            except NoSigningPower:
                raise NoSigningPower("This Ursula is a stranger and cannot be used to verify.")
        return self._interface_signature_object

    @property
    def timestamp(self):
        if not self._timestamp:
            try:
                self._sign_and_date_interface_info()
            except NoSigningPower:
                raise NoSigningPower("This Node is a Stranger; you didn't init with a timestamp, so you can't verify.")
        return self._timestamp

    def timestamp_bytes(self):
        return self.timestamp.epoch.to_bytes(4, 'big')

    @property
    def common_name(self):
        x509 = OpenSSL.crypto.X509.from_cryptography(self.certificate)
        subject_components = x509.get_subject().get_components()
        common_name_as_bytes = subject_components[0][1]
        common_name_from_cert = common_name_as_bytes.decode()
        return common_name_from_cert

    @property
    def certificate_filename(self):
        return '{}.{}'.format(self.checksum_public_address, Encoding.PEM.name.lower())  # TODO: use cert's encoding..?

    def get_certificate_filepath(self, certificates_dir: str) -> str:
        return os.path.join(certificates_dir, self.certificate_filename)

    def save_certificate_to_disk(self, directory, force=False):
        x509 = OpenSSL.crypto.X509.from_cryptography(self.certificate)
        subject_components = x509.get_subject().get_components()
        common_name_as_bytes = subject_components[0][1]
        common_name_from_cert = common_name_as_bytes.decode()

        if not self.rest_information()[0].host == common_name_from_cert:
            # TODO: It's better for us to have checked this a while ago so that this situation is impossible.  #443
            raise ValueError("You passed a common_name that is not the same one as the cert. "
                             "Common name is optional; the cert will be saved according to "
                             "the name on the cert itself.")

        certificate_filepath = self.get_certificate_filepath(certificates_dir=directory)
        _write_tls_certificate(self.certificate, full_filepath=certificate_filepath, force=force)
        self.certificate_filepath = certificate_filepath
        #self.log.info("Saved TLS certificate for {}: {}".format(self, certificate_filepath))

    @classmethod
    def from_seednode_metadata(cls,
                               seednode_metadata,
                               *args,
                               **kwargs):
        """
        Essentially another deserialization method, but this one doesn't reconstruct a complete
        node from bytes; instead it's just enough to connect to and verify a node.
        """

        return cls.from_seed_and_stake_info(checksum_address=seednode_metadata.checksum_address,
                                            host=seednode_metadata.rest_host,
                                            port=seednode_metadata.rest_port,
                                            *args, **kwargs)

    @classmethod
    def from_seed_and_stake_info(cls, host,
                                 certificates_directory,
                                 federated_only,
                                 port=9151,
                                 checksum_address=None,
                                 minimum_stake=0,
                                 network_middleware=None,
                                 *args,
                                 **kwargs
                                 ):
        if network_middleware is None:
            network_middleware = RestMiddleware()

        certificate = network_middleware.get_certificate(host=host, port=port)

        real_host = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        # Write certificate; this is really only for temporary purposes.  Ideally, we'd use
        # it in-memory here but there's no obvious way to do that.
        filename = '{}.{}'.format(checksum_address, Encoding.PEM.name.lower())
        certificate_filepath = os.path.join(certificates_directory, filename)
        _write_tls_certificate(certificate=certificate, full_filepath=certificate_filepath, force=True)
        #cls.log.info("Saved seednode {} TLS certificate".format(checksum_address))

        potential_seed_node = cls.from_rest_url(
            host=real_host,
            port=port,
            network_middleware=network_middleware,
            certificate_filepath=certificate_filepath,
            federated_only=True,
            *args,
            **kwargs)  # TODO: 466

        if checksum_address:
            if not checksum_address == potential_seed_node.checksum_public_address:
                raise potential_seed_node.SuspiciousActivity(
                    "This seed node has a different wallet address: {} (was hoping for {}).  Are you sure this is a seed node?".format(
                        potential_seed_node.checksum_public_address,
                        checksum_address))
        else:
            if minimum_stake > 0:
                # TODO: check the blockchain to verify that address has more then minimum_stake. #511
                raise NotImplementedError("Stake checking is not implemented yet.")
        try:
            potential_seed_node.verify_node(
                network_middleware=network_middleware,
                accept_federated_only=federated_only,
                certificate_filepath=certificate_filepath)
        except potential_seed_node.InvalidNode:
            raise  # TODO: What if our seed node fails verification?
        return potential_seed_node

    @classmethod
    def from_rest_url(cls,
                      network_middleware: RestMiddleware,
                      host: str,
                      port: int,
                      certificate_filepath,
                      federated_only: bool = False,
                      *args,
                      **kwargs):

        response = network_middleware.node_information(host, port, certificate_filepath=certificate_filepath)
        if not response.status_code == 200:
            raise RuntimeError("Got a bad response: {}".format(response))

        stranger_ursula_from_public_keys = cls.from_bytes(response.content, federated_only=federated_only)
        return stranger_ursula_from_public_keys

    def nickname_icon(self):
        icon_template = """
        <div class="nucypher-nickname-icon" style="border-top-color:{first_color}; border-left-color:{first_color}; border-bottom-color:{second_color}; border-right-color:{second_color};">
        <span class="symbol" style="color: {first_color}">{first_symbol}</span>
        <span class="symbol" style="color: {second_color}">{second_symbol}</span>
        <br/>
        <span class="small-address">{address_first6}</span>
        </div>
        """.replace("  ", "").replace('\n', "")
        return icon_template.format(
            first_color=self.nickname_metadata[0][0]['hex'],  # TODO: These index lookups are awful.
            first_symbol=self.nickname_metadata[0][1],
            second_color=self.nickname_metadata[1][0]['hex'],
            second_symbol=self.nickname_metadata[1][1],
            address_first6=self.checksum_public_address[2:8]
        )
