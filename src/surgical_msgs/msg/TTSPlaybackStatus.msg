# Typed acknowledgement and lifecycle status for one humanoid speech reply.
# This message reports local synthesis/playback only. It never authorizes a
# robot command and is not proof of controller admission or physical motion.
builtin_interfaces/Time stamp
uint64 sequence

string reply_id
string turn_id
string utterance_id
string procedure_run_id

# queued, waiting_function_accepted, waiting_function_completed, playing,
# played, duplicate_suppressed, or failed. Crash ambiguity is reported as
# state=failed with error_code=interrupted_unknown.
string state
# immediate, on_function_accepted, or on_function_completed.
string timing

# Included so the typed ASR admission boundary can suppress speaker echo.
string text
string text_sha256
string voice_id
string output_device
string cache_key

float32 synth_latency_ms
float32 audio_duration_sec
float32 playback_latency_ms
bool terminal
bool success
string error_code
string message
