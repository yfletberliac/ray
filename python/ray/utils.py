from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import binascii
import functools
import hashlib
import inspect
import numpy as np
import os
import subprocess
import sys
import threading
import time
import uuid

import ray.gcs_utils
import ray.raylet
import ray.ray_constants as ray_constants


def _random_string():
    id_hash = hashlib.sha1()
    id_hash.update(uuid.uuid4().bytes)
    id_bytes = id_hash.digest()
    assert len(id_bytes) == ray_constants.ID_SIZE
    return id_bytes


def format_error_message(exception_message, task_exception=False):
    """Improve the formatting of an exception thrown by a remote function.

    This method takes a traceback from an exception and makes it nicer by
    removing a few uninformative lines and adding some space to indent the
    remaining lines nicely.

    Args:
        exception_message (str): A message generated by traceback.format_exc().

    Returns:
        A string of the formatted exception message.
    """
    lines = exception_message.split("\n")
    if task_exception:
        # For errors that occur inside of tasks, remove lines 1 and 2 which are
        # always the same, they just contain information about the worker code.
        lines = lines[0:1] + lines[3:]
        pass
    return "\n".join(lines)


def push_error_to_driver(worker,
                         error_type,
                         message,
                         driver_id=None,
                         data=None):
    """Push an error message to the driver to be printed in the background.

    Args:
        worker: The worker to use.
        error_type (str): The type of the error.
        message (str): The message that will be printed in the background
            on the driver.
        driver_id: The ID of the driver to push the error message to. If this
            is None, then the message will be pushed to all drivers.
        data: This should be a dictionary mapping strings to strings. It
            will be serialized with json and stored in Redis.
    """
    if driver_id is None:
        driver_id = ray_constants.NIL_JOB_ID.id()
    data = {} if data is None else data
    worker.local_scheduler_client.push_error(
        ray.ObjectID(driver_id), error_type, message, time.time())


def push_error_to_driver_through_redis(redis_client,
                                       error_type,
                                       message,
                                       driver_id=None,
                                       data=None):
    """Push an error message to the driver to be printed in the background.

    Normally the push_error_to_driver function should be used. However, in some
    instances, the local scheduler client is not available, e.g., because the
    error happens in Python before the driver or worker has connected to the
    backend processes.

    Args:
        redis_client: The redis client to use.
        error_type (str): The type of the error.
        message (str): The message that will be printed in the background
            on the driver.
        driver_id: The ID of the driver to push the error message to. If this
            is None, then the message will be pushed to all drivers.
        data: This should be a dictionary mapping strings to strings. It
            will be serialized with json and stored in Redis.
    """
    if driver_id is None:
        driver_id = ray_constants.NIL_JOB_ID.id()
    data = {} if data is None else data
    # Do everything in Python and through the Python Redis client instead
    # of through the raylet.
    error_data = ray.gcs_utils.construct_error_message(driver_id, error_type,
                                                       message, time.time())
    redis_client.execute_command(
        "RAY.TABLE_APPEND", ray.gcs_utils.TablePrefix.ERROR_INFO,
        ray.gcs_utils.TablePubsub.ERROR_INFO, driver_id, error_data)


def is_cython(obj):
    """Check if an object is a Cython function or method"""

    # TODO(suo): We could split these into two functions, one for Cython
    # functions and another for Cython methods.
    # TODO(suo): There doesn't appear to be a Cython function 'type' we can
    # check against via isinstance. Please correct me if I'm wrong.
    def check_cython(x):
        return type(x).__name__ == "cython_function_or_method"

    # Check if function or method, respectively
    return check_cython(obj) or \
        (hasattr(obj, "__func__") and check_cython(obj.__func__))


def is_function_or_method(obj):
    """Check if an object is a function or method.

    Args:
        obj: The Python object in question.

    Returns:
        True if the object is an function or method.
    """
    return (inspect.isfunction(obj) or inspect.ismethod(obj) or is_cython(obj))


def is_class_method(f):
    """Returns whether the given method is a class_method."""
    return hasattr(f, "__self__") and f.__self__ is not None


def random_string():
    """Generate a random string to use as an ID.

    Note that users may seed numpy, which could cause this function to generate
    duplicate IDs. Therefore, we need to seed numpy ourselves, but we can't
    interfere with the state of the user's random number generator, so we
    extract the state of the random number generator and reset it after we are
    done.

    TODO(rkn): If we want to later guarantee that these are generated in a
    deterministic manner, then we will need to make some changes here.

    Returns:
        A random byte string of length ray_constants.ID_SIZE.
    """
    # Get the state of the numpy random number generator.
    numpy_state = np.random.get_state()
    # Try to use true randomness.
    np.random.seed(None)
    # Generate the random ID.
    random_id = np.random.bytes(ray_constants.ID_SIZE)
    # Reset the state of the numpy random number generator.
    np.random.set_state(numpy_state)
    return random_id


def decode(byte_str):
    """Make this unicode in Python 3, otherwise leave it as bytes."""
    if not isinstance(byte_str, bytes):
        raise ValueError("The argument must be a bytes object.")
    if sys.version_info >= (3, 0):
        return byte_str.decode("ascii")
    else:
        return byte_str


def binary_to_object_id(binary_object_id):
    return ray.ObjectID(binary_object_id)


def binary_to_hex(identifier):
    hex_identifier = binascii.hexlify(identifier)
    if sys.version_info >= (3, 0):
        hex_identifier = hex_identifier.decode()
    return hex_identifier


def hex_to_binary(hex_identifier):
    return binascii.unhexlify(hex_identifier)


def get_cuda_visible_devices():
    """Get the device IDs in the CUDA_VISIBLE_DEVICES environment variable.

    Returns:
        if CUDA_VISIBLE_DEVICES is set, this returns a list of integers with
            the IDs of the GPUs. If it is not set, this returns None.
    """
    gpu_ids_str = os.environ.get("CUDA_VISIBLE_DEVICES", None)

    if gpu_ids_str is None:
        return None

    if gpu_ids_str == "":
        return []

    return [int(i) for i in gpu_ids_str.split(",")]


def set_cuda_visible_devices(gpu_ids):
    """Set the CUDA_VISIBLE_DEVICES environment variable.

    Args:
        gpu_ids: This is a list of integers representing GPU IDs.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join([str(i) for i in gpu_ids])


def resources_from_resource_arguments(default_num_cpus, default_num_gpus,
                                      default_resources, runtime_num_cpus,
                                      runtime_num_gpus, runtime_resources):
    """Determine a task's resource requirements.

    Args:
        default_num_cpus: The default number of CPUs required by this function
            or actor method.
        default_num_gpus: The default number of GPUs required by this function
            or actor method.
        default_resources: The default custom resources required by this
            function or actor method.
        runtime_num_cpus: The number of CPUs requested when the task was
            invoked.
        runtime_num_gpus: The number of GPUs requested when the task was
            invoked.
        runtime_resources: The custom resources requested when the task was
            invoked.

    Returns:
        A dictionary of the resource requirements for the task.
    """
    if runtime_resources is not None:
        resources = runtime_resources.copy()
    elif default_resources is not None:
        resources = default_resources.copy()
    else:
        resources = {}

    if "CPU" in resources or "GPU" in resources:
        raise ValueError("The resources dictionary must not "
                         "contain the key 'CPU' or 'GPU'")

    assert default_num_cpus is not None
    resources["CPU"] = (default_num_cpus
                        if runtime_num_cpus is None else runtime_num_cpus)

    if runtime_num_gpus is not None:
        resources["GPU"] = runtime_num_gpus
    elif default_num_gpus is not None:
        resources["GPU"] = default_num_gpus

    return resources


# This function is copied and modified from
# https://github.com/giampaolo/psutil/blob/5bd44f8afcecbfb0db479ce230c790fc2c56569a/psutil/tests/test_linux.py#L132-L138  # noqa: E501
def vmstat(stat):
    """Run vmstat and get a particular statistic.

    Args:
        stat: The statistic that we are interested in retrieving.

    Returns:
        The parsed output.
    """
    out = subprocess.check_output(["vmstat", "-s"])
    stat = stat.encode("ascii")
    for line in out.split(b"\n"):
        line = line.strip()
        if stat in line:
            return int(line.split(b" ")[0])
    raise ValueError("Can't find {} in 'vmstat' output.".format(stat))


# This function is copied and modified from
# https://github.com/giampaolo/psutil/blob/5e90b0a7f3fccb177445a186cc4fac62cfadb510/psutil/tests/test_osx.py#L29-L38  # noqa: E501
def sysctl(command):
    """Run a sysctl command and parse the output.

    Args:
        command: A sysctl command with an argument, for example,
            ["sysctl", "hw.memsize"].

    Returns:
        The parsed output.
    """
    out = subprocess.check_output(command)
    result = out.split(b" ")[1]
    try:
        return int(result)
    except ValueError:
        return result


def get_system_memory():
    """Return the total amount of system memory in bytes.

    Returns:
        The total amount of system memory in bytes.
    """
    # Use psutil if it is available.
    try:
        import psutil
        return psutil.virtual_memory().total
    except ImportError:
        pass

    if sys.platform == "linux" or sys.platform == "linux2":
        # Handle Linux.
        bytes_in_kilobyte = 1024
        return vmstat("total memory") * bytes_in_kilobyte
    else:
        # Handle MacOS.
        return sysctl(["sysctl", "hw.memsize"])


def get_shared_memory_bytes():
    """Get the size of the shared memory file system.

    Returns:
        The size of the shared memory file system in bytes.
    """
    # Make sure this is only called on Linux.
    assert sys.platform == "linux" or sys.platform == "linux2"

    shm_fd = os.open("/dev/shm", os.O_RDONLY)
    try:
        shm_fs_stats = os.fstatvfs(shm_fd)
        # The value shm_fs_stats.f_bsize is the block size and the
        # value shm_fs_stats.f_bavail is the number of available
        # blocks.
        shm_avail = shm_fs_stats.f_bsize * shm_fs_stats.f_bavail
    finally:
        os.close(shm_fd)

    return shm_avail


def check_oversized_pickle(pickled, name, obj_type, worker):
    """Send a warning message if the pickled object is too large.

    Args:
        pickled: the pickled object.
        name: name of the pickled object.
        obj_type: type of the pickled object, can be 'function',
            'remote function', 'actor', or 'object'.
        worker: the worker used to send warning message.
    """
    length = len(pickled)
    if length <= ray_constants.PICKLE_OBJECT_WARNING_SIZE:
        return
    warning_message = (
        "Warning: The {} {} has size {} when pickled. "
        "It will be stored in Redis, which could cause memory issues. "
        "This may mean that its definition uses a large array or other object."
    ).format(obj_type, name, length)
    push_error_to_driver(
        worker,
        ray_constants.PICKLING_LARGE_OBJECT_PUSH_ERROR,
        warning_message,
        driver_id=worker.task_driver_id.id())


class _ThreadSafeProxy(object):
    """This class is used to create a thread-safe proxy for a given object.
        Every method call will be guarded with a lock.

    Attributes:
        orig_obj (object): the original object.
        lock (threading.Lock): the lock object.
        _wrapper_cache (dict): a cache from original object's methods to
            the proxy methods.
    """

    def __init__(self, orig_obj, lock):
        self.orig_obj = orig_obj
        self.lock = lock
        self._wrapper_cache = {}

    def __getattr__(self, attr):
        orig_attr = getattr(self.orig_obj, attr)
        if not callable(orig_attr):
            # If the original attr is a field, just return it.
            return orig_attr
        else:
            # If the orginal attr is a method,
            # return a wrapper that guards the original method with a lock.
            wrapper = self._wrapper_cache.get(attr)
            if wrapper is None:

                @functools.wraps(orig_attr)
                def _wrapper(*args, **kwargs):
                    with self.lock:
                        return orig_attr(*args, **kwargs)

                self._wrapper_cache[attr] = _wrapper
                wrapper = _wrapper
            return wrapper


def thread_safe_client(client, lock=None):
    """Create a thread-safe proxy which locks every method call
    for the given client.

    Args:
        client: the client object to be guarded.
        lock: the lock object that will be used to lock client's methods.
            If None, a new lock will be used.

    Returns:
        A thread-safe proxy for the given client.
    """
    if lock is None:
        lock = threading.Lock()
    return _ThreadSafeProxy(client, lock)


def is_main_thread():
    return threading.current_thread().getName() == "MainThread"