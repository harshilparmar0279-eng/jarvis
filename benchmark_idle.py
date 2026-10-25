"""
Benchmark idle CPU and RAM usage for JARVIS.
Measures:
1. Python base process footprint.
2. Lightweight Wake Mode (OpenWakeWord detector loaded and streaming chunks).
3. System idle baseline.
"""
import os
import sys
import time
import psutil

def measure_benchmark():
    process = psutil.Process(os.getpid())
    sys_cpu_before = psutil.cpu_percent(interval=1.0)
    
    # Base Python memory
    mem_base_rss = process.memory_info().rss / (1024 * 1024)
    print(f"[Baseline] Python process RSS: {mem_base_rss:.2f} MB")
    print(f"[Baseline] System CPU idle: {100.0 - sys_cpu_before:.1f}% idle ({sys_cpu_before:.1f}% used)")
    
    # Import wake word & startup modules
    import core.startup as startup
    import core.state_machine as sm
    import core.emergency_stop as estop
    import core.wake_word as ww
    
    mem_after_imports = process.memory_info().rss / (1024 * 1024)
    print(f"[Imported Wake Stack] Process RSS: {mem_after_imports:.2f} MB (+{mem_after_imports - mem_base_rss:.2f} MB)")
    
    # Initialize detector
    detected_flag = [False]
    def on_detect():
        detected_flag[0] = True

    print("Initializing and starting OpenWakeWord detector...")
    t0 = time.time()
    detector = ww.WakeWordDetector(on_detect=on_detect)
    started = detector.start()
    load_time = time.time() - t0
    
    mem_detector_loaded = process.memory_info().rss / (1024 * 1024)
    print(f"[Detector Loaded in {load_time:.2f}s, started={started}] Process RSS: {mem_detector_loaded:.2f} MB (Delta: +{mem_detector_loaded - mem_after_imports:.2f} MB)")
    
    # Measure CPU during active wake listening (feeding 16kHz audio chunks)
    print("Measuring CPU and memory over 5 seconds of active wake detection...")
    start_time = time.time()
    cpu_readings = []
    
    import numpy as np
    chunk = np.zeros(1280, dtype=np.int16).tobytes()
    
    # Prime cpu_percent
    process.cpu_percent(interval=None)
    
    while time.time() - start_time < 5.0:
        detector.feed(chunk)
        time.sleep(0.08) # ~80ms per chunk (1280 samples at 16kHz)
        cpu_readings.append(process.cpu_percent(interval=None))
        
    avg_proc_cpu = sum(cpu_readings) / max(1, len(cpu_readings))
    mem_running = process.memory_info().rss / (1024 * 1024)
    
    detector.stop()
    
    print("=" * 60)
    print("IDLE WAKE-WORD BENCHMARK RESULTS")
    print("=" * 60)
    print(f"Base Process RAM:           {mem_base_rss:.1f} MB")
    print(f"Wake Mode RAM (RSS):        {mem_running:.1f} MB")
    print(f"Net Wake Mode Memory Delta: +{mem_running - mem_base_rss:.1f} MB")
    print(f"Process CPU Usage (Idle):   {avg_proc_cpu:.2f}% (single-core normalized: {avg_proc_cpu / psutil.cpu_count():.2f}%)")
    print(f"System CPU Cores:           {psutil.cpu_count()} logical cores")
    print(f"Total System RAM:           {psutil.virtual_memory().total / (1024**3):.1f} GB")
    print(f"System RAM Used:            {psutil.virtual_memory().percent:.1f}%")
    print("=" * 60)

if __name__ == "__main__":
    measure_benchmark()
