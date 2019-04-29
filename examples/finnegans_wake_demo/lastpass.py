import datetime
import os
import shutil
import sys
import json
import struct
import math
from time import sleep
import re
import maya
from twisted.logger import globalLogPublisher

from nucypher.characters.lawful import Alice, Bob, Ursula
from nucypher.data_sources import DataSource as Enrico
from nucypher.network.middleware import RestMiddleware
from nucypher.utilities.logging import simpleObserver
from umbral.keys import UmbralPublicKey

# Boring setup stuff #

# Execute the download script (download_finnegans_wake.sh) to retrieve the book
BOOK_PATH = os.path.join('.', 'lastpass.json')

# Twisted Logger
globalLogPublisher.addObserver(simpleObserver)

# Temporary file storage
TEMP_FILES_DIR = "{}/examples-runtime-cruft".format(os.path.dirname(os.path.abspath(__file__)))
TEMP_DEMO_DIR = "{}/finnegans-wake-demo".format(TEMP_FILES_DIR)
TEMP_CERTIFICATE_DIR = "{}/certs".format(TEMP_DEMO_DIR)

# Remove previous demo files and create new ones
shutil.rmtree(TEMP_FILES_DIR, ignore_errors=True)
os.mkdir(TEMP_FILES_DIR)
os.mkdir(TEMP_DEMO_DIR)
os.mkdir(TEMP_CERTIFICATE_DIR)

#######################################
# Finnegan's Wake on NuCypher Testnet #
# (will fail with bad connection) #####
#######################################

TESTNET_LOAD_BALANCER = "eu-federated-balancer-40be4480ec380cd7.elb.eu-central-1.amazonaws.com"

ursula = Ursula.from_seed_and_stake_info(host=TESTNET_LOAD_BALANCER,
                                         certificates_directory=TEMP_CERTIFICATE_DIR,
                                         federated_only=True,
                                         minimum_stake=0)
policy_end_datetime = maya.now() + datetime.timedelta(days=1)
m, n = 1, 2
label = b"secret/files/and/stuff"


def json_updt(section, new_data):
    basedir = os.path.abspath(os.path.dirname(__file__))
    json_file = basedir + "/lastpass.json"  
    with open(json_file, mode='r+') as feedsjson:
        feeds = json.load(feedsjson)
        feeds[section] = str(new_data)
        feedsjson.seek(0)
        feedsjson.write(json.dumps(feeds))
        feedsjson.truncate()
    return 1

# Read data from the Writable Stream
def read_in():
    lines = sys.stdin.readlines()
    return json.loads(lines[0])

def getAlice():
    ALICE = Alice(network_middleware=RestMiddleware(),
              known_nodes=[ursula],
              learn_on_same_thread=True,
              federated_only=True,
              known_certificates_dir=TEMP_CERTIFICATE_DIR) 
    return ALICE

def getBob():
    BOB = Bob(known_nodes=[ursula],
              network_middleware=RestMiddleware(),
              federated_only=True,
              start_learning_now=True,
              learn_on_same_thread=True,
              known_certificates_dir=TEMP_CERTIFICATE_DIR) 
    return BOB

# API
def main():
    # Get method
    data_input = int(read_in())
    sys.stdout.write(str("hoihoihoi"))
    basedir = os.path.abspath(os.path.dirname(__file__))
    json_file = basedir + "/lastpass.json" 
    json_string = """"""
    sys.stdout.write(str("hoihoihoi"))
    ALICE = getAlice()
    BOB = getBob()    
    ALICE.start_learning_loop(now=True)  
    policy = ALICE.grant(BOB,
                  label,
                  m=m, n=n,
                   expiration=policy_end_datetime)
    # Alice puts her public key somewhere for Bob to find later...
    alices_pubkey_bytes_saved_for_posterity = bytes(ALICE.stamp)
    # ...and then disappears from the internet.
    del ALICE
    BOB.join_policy(label, alices_pubkey_bytes_saved_for_posterity)
    with open(BOOK_PATH, 'rb') as file:
        finnegans_wake = file.readlines()
    for counter, plaintext in enumerate(finnegans_wake):
        #########################
        # Enrico, the Encryptor #
        #########################
        enciro = Enrico(policy_pubkey_enc=policy.public_key)
        # In this case, the plaintext is a
        # single passage from James Joyce's Finnegan's Wake.
        # The matter of whether encryption makes the passage more or less readable
        # is left to the reader to determine.
        single_passage_ciphertext, _signature = enciro.encapsulate_single_message(plaintext)
        data_source_public_key = bytes(enciro.stamp)
        del enciro
        ###############
        # Back to Bob #
        ###############
        enrico_as_understood_by_bob = Enrico.from_public_keys(
            policy_public_key=policy.public_key,
            datasource_public_key=data_source_public_key,
            label=label
        )
        # Now Bob can retrieve the original message.
        alice_pubkey_restored_from_ancient_scroll = UmbralPublicKey.from_bytes(alices_pubkey_bytes_saved_for_posterity)
        delivered_cleartexts = BOB.retrieve(message_kit=single_passage_ciphertext,
                                    data_source=enrico_as_understood_by_bob,
                                    alice_verifying_key=alice_pubkey_restored_from_ancient_scroll)

        # We show that indeed this is the passage originally encrypted by Enrico.
        assert plaintext == delivered_cleartexts[0]
        json_string = json_string + format(delivered_cleartexts[0])
        json_string1 = re.sub(r'\s+', ' ', json_string) 
        json_string1 = re.sub(r'$|\t|\n|\r', '', json_string1)         
        print(json_string1.replace('b\'', '').replace('\'', '"')) 
        jso = json_string1.replace('b\'', '').replace('}\'', '}')
        #Convert string to json & render a template
        passwords = json.loads(jso)      
        sys.stdout.write(str(json.dumps(passwords)))

        
# Start the process
if __name__ == '__main__':
    main()










