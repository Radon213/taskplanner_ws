# Live AprilGrid detection statistics for a single camera.

string  camera_name

int32   processed_frames      # frames that went through the detector
int32   usable_frames         # frames where >= min_tags_per_frame tags were found
int32   tags_last_frame       # tags found in the most recently processed frame

float32 area_coverage         # 0..1, fraction of the image-area grid cells hit by a tag
float32 detect_rate_hz        # measured detector throughput for this camera

bool    ok                    # usable_frames >= min_usable_frames && area_coverage >= min_coverage
