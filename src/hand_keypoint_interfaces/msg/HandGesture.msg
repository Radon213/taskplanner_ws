# One detected hand's official MediaPipe canned-gesture result.
# hand_index is MediaPipe's frame-local result-array index, not a persistent
# track ID. If a HandKeypoints message with the same Header contains this
# index, both observations refer to the same MediaPipe hand. Gesture output is
# RGB-only, so it can exist without a depth-gated HandKeypoints counterpart.

int32 hand_index

bool has_handedness
string handedness_label
float32 handedness_score

# MediaPipe 0.10.18 exposes one winning category through its public Tasks API.
# category_name is one of the seven supported gestures listed by the parent
# HandGestureArray, or the rejection category "None". It is valid only when
# has_classification is true. ``None`` is a valid classification; false means
# MediaPipe returned no canned-gesture category for this detected hand.
bool has_classification
string category_name
float32 score
