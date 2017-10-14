import json
import os
import binascii
import uuid
import threading

import ed25519

import pyhap.util as util
from pyhap.loader import get_serv_loader

class Category:
   """
   Known category values. Category is a hint to iOS clients about what "type"
   of Accessory this represents, for UI only.
   """
   OTHER = 1
   BRIDGE = 2
   FAN = 3
   GARAGE_DOOR_OPENER = 4
   LIGHTBULB = 5
   DOOR_LOCK = 6
   OUTLET = 7
   SWITCH = 8
   THERMOSTAT = 9
   SENSOR = 10
   ALARM_SYSTEM = 11
   DOOR = 12
   WINDOW = 13
   WINDOW_COVERING = 14
   PROGRAMMABLE_SWITCH = 15
   RANGE_EXTENDER = 16
   CAMERA = 17

# Standalone accessory ID (i.e. not bridged)
STANDALONE_AID = 1

class Accessory(object):
   """
   A representation of a HAP accessory.
   
   Inherit from this class to build your own accessories.
   
   At the end of the init of this class, the _set_services method is called.
   Use this to set your HAP services.
   """

   category = Category.OTHER

   @classmethod
   def create(cls, display_name, pincode, aid=STANDALONE_AID):
      mac = util.generate_mac()
      return cls(display_name, aid=aid, mac=mac, pincode=pincode)

   def __init__(self, display_name, aid=None, mac=None, pincode=None):

      self.display_name = display_name
      self.aid = aid
      self.mac = mac
      self.config_version = 2
      self.reachable = True
      self.pincode = pincode
      self.broker = None

      sk, vk = ed25519.create_keypair()
      self.private_key = sk
      self.public_key = vk

      self.paired_clients = {}

      self.services = []
      self.iids = {} # iid: Service or Characteristic object
      self.uuids = {} # uuid: iid

      self._set_services()

   def __getstate__(self):
      state = self.__dict__.copy()
      state["broker"] = None
      return state

   def _set_services(self):
      """Sets the services for this accessory.

      The default implementation adds only the AccessoryInformation services
      and sets its Name characteristic to the Accessory's display name.
      """
      # Info service
      info_service = get_serv_loader().get("AccessoryInformation")
      info_service.get_characteristic("Name")\
                  .set_value(self.display_name, False)
      info_service.get_characteristic("Manufacturer")\
                  .set_value("Default-Manufacturer", False)
      info_service.get_characteristic("Model")\
                  .set_value("Default-Model", False)
      info_service.get_characteristic("SerialNumber")\
                  .set_value("Default-SerialNumber", False)
      self.add_service(info_service)

   def add_service(self, *servs):
      # TODO: There could be more than one services with the same UUID in the same
      # accessory. We need to distinguish them e.g. with an "artificial" subtype.
      for s in servs:
         self.services.append(s)
         iid = len(self.iids) + 1
         self.iids[iid] = s
         self.uuids[s.type_id] = iid
         for c in s.characteristics + s.opt_characteristics:
            iid = len(self.iids) + 1
            self.iids[iid] = c
            self.uuids[c.type_id] = iid
            c.broker = self

   def get_service(self, name):
      serv = next((s for s in self.services if s.display_name == name),
                  None)
      assert serv is not None
      return serv

   def set_broker(self, broker):
      self.broker = broker

   def add_paired_client(self, client_uuid, client_public):
      """Adds the given client to the set of paired clients.

      @param client_uuid: The client's UUID.
      @type client_uuid: uuid.UUID

      @param client_public: The client's public key (not the session public key).
      @type client_public: bytes
      """
      self.paired_clients[client_uuid] = client_public

   def remove_paired_client(self, client_uuid):
      """Deletes the given client from the set of paired clients.

      @param client_uuid: The client's UUID.
      @type client_uuid: uuid.UUID
      """
      self.paired_clients.pop(client_uuid)

   @property
   def paired(self):
      return len(self.paired_clients) > 0

   def get_characteristic(self, aid, iid):
      """Get's the characteristic for the given IID.

      The AID isused to verify if the search is in the correct accessory.
      """
      if aid != self.aid:
         return None

      return self.iids.get(iid)

   def to_HAP(self):
      """
      @return: A HAP representation of this accessory. For example:
         {
            "aid": 1,
            "services": [{
               "iid" 2,
               "type": ...,
               ...
            }]
         }
      @rtype: dict
      """
      services_HAP = [s.to_HAP(self.uuids) for s in self.services]
      hap_rep = {
         "aid": self.aid,
         "services": services_HAP,
      }
      return hap_rep

   def run(self):
      pass

   ### Broker

   def publish(self, data):
      """Packs the data to be send with information about this accessory and forwards it
         to this instance's broker.

      @param data: Data to publish, usually from a characteristic.
      @type data: dict

      @rtype: dict
      """
      iid = self.uuids[data["type_id"]]
      acc_data = {
         "aid": self.aid,
         "iid": iid,
         "value": data["value"],
      }
      self.broker.publish(acc_data)

class Bridge(Accessory):
   """
   A representation of a HAP bridge.
   
   A bridge can have multiple accessories.
   """

   category = Category.BRIDGE

   def __init__(self, display_name, **kwargs):
      super(Bridge, self).__init__(display_name, aid=STANDALONE_AID, **kwargs)
      self.accessories = {} # aid -> acc

   def add_accessory(self, acc):
      """Adds an accessory to this bridge.

      Bridge accessories cannot be bridged. All accessories in this bridge must have
      unique AIDs and none of them must have the STANDALONE_AID.

      @param acc: The Accessory to be bridged.
      @type acc: Accessory
      """
      if acc.category == Category.BRIDGE:
         raise ValueError("Bridges cannot be bridged")

      if acc.aid and (acc.aid == self.aid or acc.aid in self.accessories):
         raise ValueError("Duplicate AID found when attempting to add accessory")

      acc_uuid = uuid.uuid4()

      # The bridge has AID 1, start from 2 onwards
      acc.aid = len(self.accessories) + 2

      bridge_state_serv = get_serv_loader().get("BridgingState")
      bridge_state_serv.get_characteristic("AccessoryIdentifier")\
                       .set_value(acc_uuid, False)
      bridge_state_serv.get_characteristic("Reachable")\
                       .set_value(acc.reachable, False)
      bridge_state_serv.get_characteristic("Category")\
                       .set_value(acc.category, False)

      self.accessories[acc.aid] = acc

   def set_broker(self, broker):
      super(Bridge, self).set_broker(broker)
      for _, acc in self.accessories.items():
         acc.broker = broker

   def to_HAP(self):
      """Returns a HAP representation of itself and all contained accessories.

      @see: Accessory.to_HAP
      """
      hap_rep = [super(Bridge, self).to_HAP(),]

      for _, acc in self.accessories.items():
         hap_rep.append(acc.to_HAP())

      return hap_rep

   def get_characteristic(self, aid, iid):
      """@see: Accessory.to_HAP"""
      if self.aid == aid:
         return self.iids.get(iid)

      acc = self.accessories.get(aid)
      if acc is None:
         return None

      return acc.iids.get(iid)

   def run(self):
      """Creates and starts a new thread for each of the contained accessories' run
         method.
      """
      for _, acc in self.accessories.items():
         threading.Thread(target=acc.run, daemon=True).start()

def get_topic(aid, iid):
   return str(aid) + "." + str(iid)
