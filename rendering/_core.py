import numpy as np
import pyopencl as cl
import pyopencl.array as cla
import pyopencl.tools as cltools
import inspect
import math
import typing


__ctx__ = cl.create_some_context()
__queue__ = cl.CommandQueue(__ctx__)
__code__ = """
#define float4x4 float16

float4x4 transpose( float4x4 m )
{
    float4x4 t;
    // transpose
    t.even = m.lo;
    t.odd = m.hi;
    m.even = t.lo;
    m.odd = t.hi;

    return m;
}

float4 mul(float4 v, float4x4 m) {
    return (float4)(dot(v, m.even.even), dot(v, m.odd.even), dot(v, m.even.odd), dot(v, m.odd.odd));    
}

"""

float2 = cltools.get_or_register_dtype('float2')
float3 = cltools.get_or_register_dtype('float3')
float4 = cltools.get_or_register_dtype('float4')

int2 = cltools.get_or_register_dtype('int2')
int3 = cltools.get_or_register_dtype('int3')
int4 = cltools.get_or_register_dtype('int4')

uint2 = cltools.get_or_register_dtype('uint2')
uint3 = cltools.get_or_register_dtype('uint3')
uint4 = cltools.get_or_register_dtype('uint4')

float4x4 = cltools.get_or_register_dtype('float16')

RGBA = cltools.get_or_register_dtype('uchar4')

r_image1d_t = 'read_only image1d_t'
r_image2d_t = 'read_only image2d_t'
r_image3d_t = 'read_only image3d_t'

w_image1d_t = 'write_only image1d_t'
w_image2d_t = 'write_only image2d_t'
w_image3d_t = 'write_only image3d_t'



def make_float2(*args):
    if len(args) == 1 and isinstance(args[0], np.ndarray):
        return args[0].ravel().view(float2).item()
    return np.array(args, dtype=float2)


def make_float3(*args):
    if len(args) == 1 and isinstance(args[0], np.ndarray):
        args = args[0].ravel().view(np.float32)
    return np.array(tuple([*args, 0.0]), dtype=float3)


def make_float4(*args):
    if len(args) == 1 and isinstance(args[0], np.ndarray):
        return args[0].ravel().view(float4).item()
    return np.array(args, dtype=float4)


def make_float4x4(*args):
    if len(args) == 1 and isinstance(args[0], np.ndarray):
        return args[0].ravel().view(float4x4).item()
    return np.array(args, dtype=float4x4)


def to_array(v):
    v_shape = v.shape
    if v.shape == ():
        v = np.expand_dims(v, 0)
    if v.dtype == float2:
        return v.view(np.float32).reshape(*v_shape, 2)
    elif v.dtype == float3:
        return v.view(np.float32).reshape(*v_shape, 4)[..., 0:3]
    elif v.dtype == float4:
        return v.view(np.float32).reshape(*v_shape, 4)
    elif v.dtype == float4x4:
        return v.view(np.float32).reshape(*v_shape, 4, 4)
    return v


def _get_signature(f):
    signature = inspect.signature(f)
    assert all(v.annotation != inspect.Signature.empty for v in signature.parameters.values()), "All arguments needs to be annotated with a type descriptor"
    return [(k, v) for k, v in signature.parameters.items()], signature.return_annotation


__OBJECT_TYPE_TO_CLTYPE__ = {
    cl.mem_object_type.IMAGE1D: 'image1d_t',
    cl.mem_object_type.IMAGE2D: 'image2d_t',
    cl.mem_object_type.IMAGE3D: 'image3d_t',
    cl.mem_object_type.BUFFER: 'buffer_t',
}


def _get_annotation_as_cltype(annotation, assert_no_pointer=False):
    if annotation is None:
        return "void"
    is_pointer = False
    if isinstance(annotation, list):
        assert len(annotation) == 1, "parameters annotated with list should refer to a pointer to a single type, e.g. [int] is considered a int*."
        is_pointer = True
        annotation = annotation[0]
    if isinstance(annotation, str):  # object types
        return annotation
    if assert_no_pointer:
        assert not is_pointer, "Can not use pointers in kernel auxiliary functions"
    return ("__global " if is_pointer else "")+cltools.dtype_to_ctype(annotation) +("*" if is_pointer else "")


def kernel_function(f):
    s, return_annotation = _get_signature(f)
    name = f.__name__
    global __code__

    __code__ += f"""
{_get_annotation_as_cltype(return_annotation, assert_no_pointer=True)} {name}({', '.join(_get_annotation_as_cltype(v.annotation, assert_no_pointer=True) + " " + v.name for k, v in s)}) {{
{inspect.getdoc(f)}
}}
"""
    def wrapper(*args):
        raise Exception("Can not call to this function from host.")
    return wrapper


def kernel_main(f):
    s, return_annotation = _get_signature(f)
    assert return_annotation == inspect.Signature.empty, "Kernel main function must return void"
    name = f.__name__
    global __code__
    __code__ += f"""
__kernel void {name}({', '.join(_get_annotation_as_cltype(v.annotation)+" "+v.name for k,v in s)}) {{
int thread_id = get_global_id(0);
{inspect.getdoc(f)}
}}
    """
    # print(__code__)
    program = None
    class Dispatcher:
        def __init__(self):
            pass
        def __getitem__(self, num_threads):
            if isinstance(num_threads, list) or isinstance(num_threads, tuple):
                num_threads = math.prod(num_threads)
            def resolve_arg(a, annotation):
                if isinstance(a, cla.Array):
                    if isinstance(annotation, list):
                        a = a.data  # case of a pointer, pass the buffer
                    else:
                        a = a.get()  # pass the numpy array as a value transfer.
                if isinstance(a, int):
                    a = np.int32(a)
                return a
            def dispatch_call(*args):
                nonlocal program
                if program is None:
                    program = cl.Program(__ctx__, __code__).build()
                kernel = program.__getattr__(name)
                kernel(__queue__, (num_threads,), None, *[resolve_arg(a, v.annotation) for a, (k,v) in zip(args,s)])
            return dispatch_call
    return Dispatcher()


def kernel_struct(cls):
    fields = cls.__dict__['__annotations__']
    assert all(k in fields.keys() for k in cls.__dict__.keys() if k[0] != "_"), "A public field was declared without annotation"
    dtype = np.dtype([(k, v) for k,v in fields.items()])
    dtype, cltype = cltools.match_dtype_to_c_struct(__ctx__.devices[0], cls.__name__, dtype)
    global __code__
    __code__ += cltype
    cltools.get_or_register_dtype(cls.__name__, dtype)
    return dtype


def create_buffer(count: int, dtype: np.dtype):
    return cla.zeros(__queue__, (count,), dtype)


def create_buffer_from(ary: np.ndarray):
    return cla.to_device(__queue__, ary)


def create_struct(dtype: np.dtype):
    return cla.zeros(__queue__, 1, dtype)[0]


def create_struct_from(ary: np.ndarray):
    return cla.to_device(__queue__, ary.item())


__IMAGE_FORMATS__ = {
    float4: cl.ImageFormat(cl.channel_order.RGBA, cl.channel_type.FLOAT),
    float3: cl.ImageFormat(cl.channel_order.RGB, cl.channel_type.FLOAT),
    float2: cl.ImageFormat(cl.channel_order.RG, cl.channel_type.FLOAT),
    np.float32: cl.ImageFormat(cl.channel_order.R, cl.channel_type.FLOAT),
    RGBA: cl.ImageFormat(cl.channel_order.BGRA, cl.channel_type.UNORM_INT8)
}


__CHANNEL_TYPE_TO_DTYPE__ = {
    cl.channel_type.FLOAT: np.float32,
    cl.channel_type.SIGNED_INT32: np.int32,
    cl.channel_type.SIGNED_INT8: np.int8,
    cl.channel_type.UNSIGNED_INT32: np.uint32,
    cl.channel_type.UNSIGNED_INT8: np.uint8,
    cl.channel_type.UNORM_INT8: np.int8
}


__CHANNEL_ORDER_TO_COMPONENTS__ =  {
    cl.channel_order.BGRA: 4,
    cl.channel_order.RGBA: 4,
    cl.channel_order.RGB: 3,
    cl.channel_order.RG: 2,
    cl.channel_order.R: 1
}


def get_valid_image_formats():
    return __IMAGE_FORMATS__.keys()


Image = cl.Image
Buffer = cl.Buffer


def create_image2d(width: int, height: int, dtype: np.dtype):
    assert dtype in __IMAGE_FORMATS__, "Unsupported dtype for image format"
    return cl.Image(__ctx__, cl.mem_flags.READ_WRITE, __IMAGE_FORMATS__[dtype], shape=(width, height))


def clear(b, value = np.float32(0)):
    if not isinstance(value, np.ndarray):
        value = np.array(value)
    if isinstance(b, Buffer):
        cl.enqueue_fill_buffer(__queue__, b, value, 0, value.nbytes)
    else:
        if math.prod(value.shape) <= 1:
            value = np.array([value]*4)
        cl.enqueue_fill_image(__queue__, b, value, (0,0,0), (b.width, max(1, b.height), max(1, b.depth)))


def mapped(b: typing.Union[cla.Array, Buffer, Image]):
    class _ctx:
        def __init__(self):
            self.mapped = None
        def __enter__(self):
            if isinstance(b, cl.Image):
                dtype = __CHANNEL_TYPE_TO_DTYPE__[b.format.channel_data_type]
                cmps = __CHANNEL_ORDER_TO_COMPONENTS__[b.format.channel_order]
                shape = (b.depth, b.height, b.width, cmps)
                if b.type < cl.mem_object_type.IMAGE3D:
                    shape = shape[1:]
                if b.type < cl.mem_object_type.IMAGE2D:
                    shape = shape[1:]
                if cmps == 1:
                    shape = shape[:-1]
                self.mapped = cl.enqueue_map_image(__queue__, b, cl.map_flags.READ | cl.map_flags.WRITE,
                                                    (0,0,0), (b.width, max(1, b.height), max(1, b.depth)), shape=shape, dtype=dtype)
            elif isinstance(b, cl.Buffer):
                self.mapped = cl.enqueue_map_buffer(__queue__, b, cl.map_flags.READ | cl.map_flags.WRITE,
                                                    b.offset, (b.size,), np.uint8)
            else:
                self.mapped = cl.enqueue_map_buffer(__queue__, b.base_data, cl.map_flags.READ | cl.map_flags.WRITE,
                                                    b.offset, b.shape, b.dtype)
            return self.mapped[0]
        def __exit__(self, exc_type, exc_val, exc_tb):
            self.mapped[0].base.release()
    return _ctx()


def identity():
    return make_float4x4(
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0
    )


def translate(*args):
    if len(args) == 1:
        x, y, z = to_array(args[0])
    else:
        x, y, z = args
    return make_float4x4(
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        x, y, z, 1.0
    )


def scale(*args):
    if len(args) == 1:
        if np.isscalar(args[0]):
            x, y, z = args[0], args[0], args[0]
        else:
            x, y, z = to_array(args[0])
    else:
        x, y, z = args
    return make_float4x4(
        x, 0.0, 0.0, 0.0,
        0.0, y, 0.0, 0.0,
        0.0, 0.0, z, 0.0,
        0.0, 0.0, 0.0, 1.0
    )


def rotate(angle, axis):
    cos = np.cos(angle)
    sin = np.sin(angle)
    x, y, z = to_array(axis)
    return make_float4x4(
        x * x * (1 - cos) + cos, y * x * (1 - cos) + z * sin, z * x * (1 - cos) - y * sin, 0,
        x * y * (1 - cos) - z * sin, y * y * (1 - cos) + cos, z * y * (1 - cos) + x * sin, 0,
        x * z * (1 - cos) + y * sin, y * z * (1 - cos) - x * sin, z * z * (1 - cos) + cos, 0,
        0, 0, 0, 1
    )


__COMPONENTS_FROM_TYPE__ = {
    float2: 2,
    float3: 3,
    float4: 4
}


def matmul(a, b):
    assert a.dtype == float4 or a.dtype == float4x4, "First vector must be a float4 or a matrix float4x4"
    assert b.dtype == float4x4, "Second argument must be a matrix"
    a_is_vec = a.dtype == float4
    a = to_array(a)
    b = to_array(b)
    c = a @ b
    if a_is_vec:
        return make_float4(c)
    else:
        return make_float4x4(c)


def dot(v1, v2):
    assert v1.dtype == v2.dtype, "Can not apply dot product between different vector types"
    assert v1.shape == v2.shape, "Can not apply dot product between different vector types"
    if v1.dtype == float2:
        return (v1['x']*v2['x']+v1['y']*v2['y']).item()
    elif v1.dtype == float3:
        return (v1['x']*v2['x']+v1['y']*v2['y'] + v1['z']*v2['z']).item()
    elif v1.dtype == float4:
        return (v1['x']*v2['x']+v1['y']*v2['y'] + v1['z']*v2['z'] + v1['w']*v2['w']).item()
    raise Exception('Not valid dtype')


def normalize(v):
    v_dtype = v.dtype
    l = np.sqrt(dot(v, v))
    v = to_array(v) / l
    if v_dtype == float3:
        return make_float3(v)
    else:
        return (v / l).view(v_dtype)


def cross(v1, v2):
    return make_float3(
        (v1['y'] * v2['z'] - v1['z'] * v2['y']).item(),
        (v1['z'] * v2['x'] - v1['x'] * v2['z']).item(),
        (v1['x'] * v2['y'] - v1['y'] * v2['x']).item()
    )


def direction(f, t):
    f = to_array(f)
    t = to_array(t)
    d = make_float3(t - f)
    return normalize(d)


def look_at(camera, target, up_vector):
    zaxis = direction(camera, target)
    xaxis = normalize(cross(up_vector, zaxis))
    yaxis = cross(zaxis, xaxis)
    return make_float4x4(
        xaxis['x'], yaxis['x'], zaxis['x'], 0,
        xaxis['y'], yaxis['y'], zaxis['y'], 0,
        xaxis['z'], yaxis['z'], zaxis['z'], 0,
        -dot(xaxis, camera), -dot(yaxis, camera), -dot(zaxis, camera), 1
    )


def perspective(fov = 3.141593 / 4, aspect_ratio = 1.0, znear = .01, zfar = 100.0):
    hs = 1.0 / np.tan(fov / 2)
    ws = hs / aspect_ratio
    return make_float4x4(
        ws, 0, 0, 0,
        0, hs, 0, 0,
        0, 0, zfar / (zfar - znear), 1.0,
        0, 0, -znear * zfar / (zfar - znear), 0
    )


