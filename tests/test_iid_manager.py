from unittest.mock import Mock

from pyhap.iid_manager import IIDManager


def get_iid_manager():
    obj_a = Mock()
    iid_manager = IIDManager()
    iid_manager.assign(obj_a)
    return iid_manager, obj_a


def test_assign():
    iid_manager, obj_a = get_iid_manager()
    obj_b = Mock()
    iid_manager.assign(obj_b)
    assert iid_manager.iids == {obj_a: 1, obj_b: 2}
    iid_manager.assign(obj_a)
    assert iid_manager.iids == {obj_a: 1, obj_b: 2}


def test_assign_order_with_remove():
    iid_manager, obj_a = get_iid_manager()
    assert iid_manager.remove_obj(obj_a) == 1
    iid_manager.assign(obj_a)
    assert iid_manager.iids == {obj_a: 2}


def test_get_obj():
    iid_manager, obj_a = get_iid_manager()
    assert iid_manager.get_obj(1) == obj_a
    assert iid_manager.get_obj(0) is None


def test_get_iid():
    iid_manager, obj_a = get_iid_manager()
    assert iid_manager.get_iid(obj_a) == 1
    assert iid_manager.get_iid(Mock()) is None


def test_remove_obj():
    iid_manager, obj_a = get_iid_manager()
    assert iid_manager.remove_obj(Mock()) is None
    assert iid_manager.remove_obj(obj_a) == 1

def test_remove_iid():
    iid_manager, obj_a = get_iid_manager()
    assert iid_manager.remove_iid(0) is None
    assert iid_manager.remove_iid(1) == obj_a
