Configuration for ingest must be specified in a YaML file such as this:

```
data_source:
  EDGES: ""
  COMPONENTS: ""
  WATERSHED: ""

graph_config:
  CHUNK_SIZE: [X, Y, Z]
  FANOUT: <int>
  SPATIAL_BITS: <int>
  LAYER_ID_BITS: <int>

backend_client:
  TYPE: "bigtable"
  CONFIG:
    ADMIN: true
    READ_ONLY: false

mesh_config:
  dir: "graphene_meshes"
  mip: <int>
  max_layer: <int>
  max_error: <int>
  chunk_size: [X, Y, Z]
  minishard_bits: {2: 1, 3: 3, 4: 6}
```

### `data_source`
* `EDGES` - path to edge files.
* `COMPONENTS` - path to component files.
* `WATERSHED` - path to flat segmentation for which the chunkedgraph is being created. Must have an `info` file in the path that specifies volume size among other things.

The protocol for these paths must be supported by [cloud-files](https://github.com/seung-lab/cloud-files/) or [cloud-volume](https://github.com/seung-lab/cloud-volume/).

### `graph_config`
* `CHUNK_SIZE` - atomic chunk size in [x, y, z].
* `FANOUT` - number of chunks in each axis that form a larger parent chunk.
* `SPATIAL_BITS` - number of bits in segment IDs reserved per axis for chunk coordinates.
* `LAYER_ID_BITS` - number of bits in segment IDs reserved for layer.

For more information refer to this well documented [graphene](https://github.com/seung-lab/cloud-volume/wiki/Graphene) section in the cloud-volume wiki, courtesy of Will Silversmith.

### `backend_client`
This can be left as is. Currently bigtable is the only supported backend to store the chunkedgraph.

### `mesh_config`
* `dir` - mesh directory inside the watershed CloudVolume (e.g. `graphene_meshes`).
* `mip` - mip level to mesh at.
* `max_layer` - highest layer to mesh and stitch up to.
* `max_error` - marching-cubes simplification error.
* `chunk_size` - mesh chunk size in [x, y, z] (mip-adjusted).
* `minishard_bits` - sharded-mesh minishard bits per layer, `{layer: bits}`.
* `dynamic_mesh_dir` - directory for dynamically-created (post-edit) meshes. Use `dynamic` on `main`; on the `pcgv3` branch the graph id is appended automatically (default `dynamic_<graph_id>`).
