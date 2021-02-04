import tensorflow as tf
from absl.flags import FLAGS

def load_tfrecord_dataset(file_path):
    dataset = tf.data.TFRecordDataset(file_path)

    # Parse the TFRecord file
    dataset = dataset.map(parse_tf_example)

    return dataset
# Create a dictionary describing the features.

# Create a dictionary describing the features.
image_feature_description = {
    'filename': tf.io.FixedLenFeature([], tf.string),
    'height': tf.io.FixedLenFeature([], tf.int64),
    'width': tf.io.FixedLenFeature([], tf.int64),
    'classes_id': tf.io.VarLenFeature(tf.int64),
    'classes_name': tf.io.VarLenFeature(tf.string),
    'x_mins': tf.io.VarLenFeature(tf.float32),
    'y_mins': tf.io.VarLenFeature(tf.float32),
    'x_maxes': tf.io.VarLenFeature(tf.float32),
    'y_maxes': tf.io.VarLenFeature(tf.float32),
    'image_raw': tf.io.FixedLenFeature([], tf.string),
}

def parse_tf_example(example_proto):
    # Parse the input tf.Example proto using the dictionary above.
    parsed_example = tf.io.parse_single_example(example_proto, image_feature_description)
    
    x_train = tf.image.decode_jpeg(parsed_example['image_raw'], channels=3)
    
    labels = tf.sparse.to_dense(parsed_example['classes_id'])
    labels = tf.cast(labels, tf.float32)
    
    xmin = tf.sparse.to_dense(parsed_example['x_mins'])
    ymin = tf.sparse.to_dense(parsed_example['y_mins'])
    xmax = tf.sparse.to_dense(parsed_example['x_maxes'])
    ymax = tf.sparse.to_dense(parsed_example['y_maxes'])
    
    y_train = tf.stack([xmin, ymin, xmax, ymax, labels], axis=1)
    
    return x_train, y_train

@tf.function
def transform_bboxes_for_output(y_true, grid_size, anchor_idxs, best_anchor_idx):
    # y_true: (max_boxes, [x1, y1, x2, y2, classes])
    N = tf.shape(y_true)[0]
  
    # y_true_out: (grid, grid, anchors, [x, y, w, h, obj, class])
    y_true_out = tf.zeros(
        (grid_size, grid_size, tf.shape(anchor_idxs)[0], 6))
    
    anchor_idxs = tf.cast(anchor_idxs, tf.int32)
    
    indices = tf.TensorArray(tf.int32, 1, dynamic_size=True)
    updates = tf.TensorArray(tf.float32, 1, dynamic_size=True)
    
    # Find which grid includes the center of object
    #boxes_xy = y_true[..., 0:2]
    #grid_xy = tf.cast(boxes_xy // (1 / grid_size), tf.int32)

    for i in range(N):
        if tf.equal(y_true[i][0], 0):
            continue
        
        anchor_eq = tf.equal(
            anchor_idxs, best_anchor_idx[i][0])
        
        if tf.reduce_any(anchor_eq):
            # Find which grid includes the center of object
            boxes_xy = (y_true[i][0:2] + y_true[i][2:4]) / 2
            grid_xy = tf.cast(boxes_xy // (1 / grid_size), tf.int32)
            
            anchor_idx = tf.cast(tf.where(anchor_eq), tf.int32)

            indices = indices.write(i, [grid_xy[1], grid_xy[0], anchor_idx[0][0]])
            updates = updates.write(i, [y_true[i][0], y_true[i][1], y_true[i][2], y_true[i][3], 1, y_true[i][4]])
        
    #tf.print("indices: ", indices.stack())
    #tf.print("updates: ", updates.stack())
    
    return tf.tensor_scatter_nd_update(
        y_true_out, indices.stack(), updates.stack())

def transform_bboxes(y_train, anchors, anchor_masks, image_size=416):
    """Area of anchor."""
    # anchors shape: (9, 2)
    # Note: normalization anchors to 0~1 (anchors / 416)
    #       -> anchors and boxes_wh are moved to origin point
    #       -> we can conveniently find the minimum 
    #          between anchors and boxes_wh to find the intersection area.
    anchors = tf.cast(anchors, tf.float32)
    anchor_area = anchors[..., 0] * anchors[..., 1] # (9,)
    
    # y_train shape: (N, (x1, y1, x2, y2, classes))
    boxes_wh = y_train[..., 2:4] - y_train[..., 0:2] # (N, 2)
    
    # expand dimension for compare with anchor
    boxes_wh = tf.tile(tf.expand_dims(boxes_wh, -2),
                     (1, tf.shape(anchors)[0], 1)) # (N, 9, 2)
    boxes_area = boxes_wh[..., 0] * boxes_wh[..., 1] # (N, 9)
    
    """Find IOU between box shifted to origin and anchor box."""    
    intersection = tf.minimum(boxes_wh[..., 0], anchors[..., 0]) * \
        tf.minimum(boxes_wh[..., 1], anchors[..., 1]) # (N, 9)
    iou = intersection / (boxes_area + anchor_area - intersection) # (N, 9)
    
    """Find the best iou."""
    best_anchor_idx = tf.cast(tf.argmax(iou, axis=-1), tf.float32)
    best_anchor_idx = tf.expand_dims(best_anchor_idx, -1) # (N, 1)
    best_anchor_idx = tf.cast(best_anchor_idx, tf.int32)
    
    """Find which grid includes the center of object."""
    y_outs = []
    grid_size = image_size // 32

    for anchor_idxs in anchor_masks:
        y_outs.append(transform_bboxes_for_output(
            y_train, grid_size, anchor_idxs, best_anchor_idx))
        grid_size *=2

    return tuple(y_outs)

# Preprocess the dataset
def preprocess_data(x_train, y_train, anchors, anchor_masks, image_size=416, yolo_max_boxes=100):
    # Resize the image data.
    x_train = tf.image.resize(x_train, (image_size, image_size))
    x_train /= 255.

    # Origin boxes: (xmin, ymin, xmax, ymax, classes)
    """Add zero pad for training

    paddings = [[row_top, row_bottom], [col_left, col_right]]
    """
    paddings = [[0, yolo_max_boxes - tf.shape(y_train)[0]], [0, 0]]
    y_train = tf.pad(y_train, paddings)

    y_train = transform_bboxes(y_train, anchors, anchor_masks, image_size)

    return x_train, y_train

