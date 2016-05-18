# -*- coding: utf8 -*-
import pytest


from ethereum import utils
from ethereum import tester
from ethereum.utils import sha3, privtoaddr
from ethereum.tester import TransactionFailed

library_code = '''

contract ChannelManagerContract {
    address public assetAddress;
    
    function ChannelManagerContract(address testAddress) {
        assetAddress = testAddress;
    }
}

library IterableMappingCMC {
    // Might have to define the NettingContract type here for insertion
    struct itmap {
        mapping(address => IndexValue) data;
        KeyFlag[] keys;
        uint size;
    }
    struct IndexValue { uint keyIndex; ChannelManagerContract value; }
    struct KeyFlag { address key; bool deleted; }


    function insert(itmap storage self, address key, ChannelManagerContract value) returns (bool replaced) {
        uint keyIndex = self.data[key].keyIndex;
        self.data[key].value = value;
        if (keyIndex > 0)
            return true;
        else {
            keyIndex = self.keys.length++;
            self.data[key].keyIndex = keyIndex + 1;
            self.keys[keyIndex].key = key;
            self.size++;
            return false;
        }
    }


    function remove(itmap storage self, address key) returns (bool success){
        uint keyIndex = self.data[key].keyIndex;
        if (keyIndex == 0) return false;
        delete self.data[key];
        self.keys[keyIndex - 1].deleted = true;
        self.size --;
    }


    function contains(itmap storage self, address key) returns (bool) {
        return self.data[key].keyIndex > 0;
    }


    function atIndex(itmap storage self, address key) returns (uint index) {
        return self.data[key].keyIndex;
    }


    function iterate_start(itmap storage self) returns (uint keyIndex){
        return iterate_next(self, uint(-1));
    }


    function iterate_valid(itmap storage self, uint keyIndex) returns (bool){
        return keyIndex < self.keys.length;
    }


    function iterate_next(itmap storage self, uint keyIndex) returns (uint r_keyIndex){
        keyIndex++;
        while (keyIndex < self.keys.length && self.keys[keyIndex].deleted)
            keyIndex++;
        return keyIndex;
    }


    function iterate_get(itmap storage self, uint keyIndex) returns (address key, ChannelManagerContract value){
        key = self.keys[keyIndex].key;
        value = self.data[key].value;
    }
}

'''

registry_code = '''
library IterableMappingCMC {
    // Might have to define the NettingContract type here for insertion
    struct itmap {
        mapping(address => IndexValue) data;
        KeyFlag[] keys;
        uint size;
    }
    struct IndexValue { uint keyIndex; ChannelManagerContract value; }
    struct KeyFlag { address key; bool deleted; }


    function insert(itmap storage self, address key, ChannelManagerContract value) returns (bool replaced) {
        uint keyIndex = self.data[key].keyIndex;
        self.data[key].value = value;
        if (keyIndex > 0)
            return true;
        else {
            keyIndex = self.keys.length++;
            self.data[key].keyIndex = keyIndex + 1;
            self.keys[keyIndex].key = key;
            self.size++;
            return false;
        }
    }


    function remove(itmap storage self, address key) returns (bool success){
        uint keyIndex = self.data[key].keyIndex;
        if (keyIndex == 0) return false;
        delete self.data[key];
        self.keys[keyIndex - 1].deleted = true;
        self.size --;
    }


    function contains(itmap storage self, address key) returns (bool) {
        return self.data[key].keyIndex > 0;
    }


    function atIndex(itmap storage self, address key) returns (uint index) {
        return self.data[key].keyIndex;
    }


    function iterate_start(itmap storage self) returns (uint keyIndex){
        return iterate_next(self, uint(-1));
    }


    function iterate_valid(itmap storage self, uint keyIndex) returns (bool){
        return keyIndex < self.keys.length;
    }


    function iterate_next(itmap storage self, uint keyIndex) returns (uint r_keyIndex){
        keyIndex++;
        while (keyIndex < self.keys.length && self.keys[keyIndex].deleted)
            keyIndex++;
        return keyIndex;
    }


    function iterate_get(itmap storage self, uint keyIndex) returns (address key, ChannelManagerContract value){
        key = self.keys[keyIndex].key;
        value = self.data[key].value;
    }
}
contract ChannelManagerContract {
    address public assetAddress;
    
    function ChannelManagerContract(address testAddress) {
        assetAddress = testAddress;
    }
}

contract Registry {
    IterableMappingCMC.itmap data;


    /// @notice addAsset(address) to add a new ChannelManagerContract to channelManagerContracts
    /// with the assetAddress as key.
    /// @dev Add a new ChannelManagerContract to channelManagerContracts if assetAddress 
    /// does not already exist.
    /// @param assetAddress (address) the address of the asset
    /// @return nothing, but updates the collection of ChannelManagerContracts.
    function addAsset(address assetAddress) returns (ChannelManagerContract c) {
        // only allow unique addresses
        if (IterableMappingCMC.contains(data, assetAddress)) throw;
        c = new ChannelManagerContract(assetAddress);
        IterableMappingCMC.insert(data, assetAddress, c);
    }


    /// @notice channelManagerByAsset(address) to get the ChannelManagerContract
    /// of the given assetAddress.
    /// @dev Get the ChannelManagerContract of a given assetAddress.
    /// @param assetAddress (address) the asset address.
    /// @return contractAddr (address) the address of channelManagerContract.
    /// @return assetAddr (address) the address of asset.
    function channelManagerByAsset(address assetAddress) returns (address contractAddr, address assetAddr) {
        // if assetAddress does not exist, throw
        if (IterableMappingCMC.contains(data, assetAddress) == false) throw;
        uint index = IterableMappingCMC.atIndex(data, assetAddress);
        var(key, value) = IterableMappingCMC.iterate_get(data, index - 1);
        contractAddr = value;
        assetAddr = value.assetAddress();
    }


    /// @notice assetAddresses() to get all assetAddresses in the collection.
    /// @dev Get all assetAddresses in the collection.
    /// @return assetAddress (address[]) an array of all assetAddresses
    function assetAddresses() returns (address[] assetAddresses) {
        assetAddresses = new address[](data.size);
        for (var i = IterableMappingCMC.iterate_start(data); IterableMappingCMC.iterate_valid(data, i); i = IterableMappingCMC.iterate_next(data, i)) {
            var (key, value) = IterableMappingCMC.iterate_get(data, i);
            assetAddresses[i] = key;
        }
    }

    // empty function to handle wrong calls
    function () { throw; }
}
'''


def test_registry():
    s = tester.state()
    assert s.block.number < 1150000
    s.block.number = 1158001
    assert s.block.number > 1150000
    lib_c = s.abi_contract(library_code, language="solidity")
    c = s.abi_contract(registry_code, language="solidity", libraries={'IterableMappingCMC': lib_c.address.encode('hex')})

    assetAdr = c.addAsset(sha3('asset')[:20])
    print(assetAdr)
    c.addAsset(sha3('address')[:20])
    # if address already exists, throw
    with pytest.raises(TransactionFailed):
        c.addAsset(sha3('asset')[:20])

    cmc = c.channelManagerByAsset(sha3('asset')[:20])
    assert cmc[0] == assetAdr # first item is the contract address
    assert cmc[1] == sha3('asset')[:20].encode('hex') # second item is the assetAddress
    # if address does not exist, throw
    with pytest.raises(TransactionFailed):
        c.channelManagerByAsset(sha3('mainz')[:20])

    adrs = c.assetAddresses()
    assert len(adrs) == 2
    assert adrs[0] == sha3('asset')[:20].encode('hex')
    assert adrs[1] == sha3('address')[:20].encode('hex')
