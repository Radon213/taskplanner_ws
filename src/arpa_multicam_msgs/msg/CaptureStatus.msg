# Aggregated live status of the capture session, published continuously.

std_msgs/Header header

# --- rig bring-up ---------------------------------------------------------
# Cameras that delivered a frame within `online_timeout_sec`. The rig is only
# usable once every expected camera is in this list; auto-start waits for it.
string[] online_cameras
string[] offline_cameras
bool     all_cameras_online
float64  uptime_sec            # seconds since all expected cameras came online (0 = not yet)

# --- session --------------------------------------------------------------
bool    recording
string  session_name
string  capture_dir
float64 elapsed_sec          # main data bag, never cut by a calibration

# The separate calibration-only bag. "c" closes this one and opens the next;
# the main recording above is unaffected.
string  calib_bag_uri
float64 calib_bag_elapsed_sec
int32   calib_bag_index

CameraCoverage[] cameras

# Frames in which at least `min_cams_per_frame` cameras saw the grid simultaneously.
# This is what actually constrains the inter-camera extrinsics.
int32   multi_cam_frames

# Per-PAIR shared views. Kalibr initialises the rig pairwise, so one weak pair
# fails the whole solve even when every individual camera looks healthy.
# pair_names[i] is "camA|camB" and pair_frames[i] its count.
string[] pair_names
int32[]  pair_frames
int32    min_pair_frames      # threshold each pair must reach

# Approximate-sync health
int32   synced_frames
float64 max_sync_skew_ms      # worst stamp spread inside the latest synced set

bool    ready_for_calibration
string  hint                  # human readable "what is still missing"

# --- calibration / tf state ----------------------------------------------
bool     calibrating           # a calibrate_extrinsics goal is executing
string   calibration_stage     # mirrors the action feedback stage, '' when idle
float32  calibration_progress  # 0..1
string   extrinsics_json       # the JSON currently broadcast ('' if none)
string[] published_frames      # child frames currently on /tf_static
