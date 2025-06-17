from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
import multiprocessing
import time
import os
import glob
import threading
import cv2
import numpy as np
import pymysql as mysql
from datetime import datetime

def cleanup_old_segments(max_segments=10):
    try:
        ts_files = glob.glob("/mnt/camera/*.ts")
        
        if len(ts_files) > max_segments:
            ts_files.sort(key=os.path.getmtime)
            files_to_delete = ts_files[:-max_segments]
            for file_path in files_to_delete:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                    
    except Exception:
        pass

def periodic_cleanup(interval=30):
    while True:
        time.sleep(interval)
        cleanup_old_segments()

def insert_motion_alert(camera_id):
    try:
        conn = mysql.connect(
            host='172.22.0.2',
            user='root',
            password='CIEL2',
            database='BDD_Projet_1'
        )
        cursor = conn.cursor()
        
        type_alerte = "Mouvement détecté"
        message = f"Mouvement détecté à {datetime.now().strftime('%H:%M:%S')}"
        niveau_criticite = "Moyen"
        date_creation = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        query = """INSERT INTO alerte (type_alerte, message, niveau_criticite, date_creation) 
                   VALUES (%s, %s, %s, %s)"""
        cursor.execute(query, (type_alerte, message, niveau_criticite, date_creation))
        
        conn.commit()
        cursor.close()
        conn.close()
        
    except Exception as e:
        print(f"Erreur base de données: {e}")

def stream_camera_ai(index, output_name, noir_queue):
    """Caméra IA avec détection de luminosité ET de mouvement"""
    picam = Picamera2(index)
    config = picam.create_video_configuration(main={"size": (1920, 1080), "format": "RGB888"})
    picam.configure(config)

    encoder = H264Encoder()
    output = FfmpegOutput(f"/mnt/camera/{output_name}", [
        "-f", "hls",
        "-hls_time", "10",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list+split_by_time",
        "-hls_allow_cache", "0",
        "-b:v", "4000k",
        "-maxrate", "5000k",
        "-bufsize", "8000k",
        "-preset", "faster",
        "-tune", "zerolatency"
    ])

    # Lance le thread de nettoyage
    cleanup_thread = threading.Thread(target=periodic_cleanup, args=(30,))
    cleanup_thread.daemon = True
    cleanup_thread.start()
    print("Nettoyage automatique activé")

    # Variables pour la détection de mouvement
    previous_frame = None
    last_motion_alert = 0  # Timestamp de la dernière alerte
    motion_cooldown = 240  # 4 minutes en secondes

    # Thread de détection luminosité + mouvement
    def detect_brightness_and_motion():
        nonlocal previous_frame, last_motion_alert
        
        while True:
            try:
                frame = picam.capture_array("main")
                gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
                
                # 1. DÉTECTION DE LUMINOSITÉ (existant)
                brightness = np.mean(gray)
                
                if brightness > 60:  # Lumière allumée
                    mode = "normal"
                else:  # Lumière éteinte
                    mode = "ir"
                
                if not noir_queue.full():
                    noir_queue.put(mode)
                
                # 2. DÉTECTION DE MOUVEMENT (nouveau)
                current_time = time.time()
                
                # Vérifier le cooldown (4 minutes entre alertes)
                if current_time - last_motion_alert >= motion_cooldown:
                    
                    if previous_frame is not None:
                        # Redimensionner pour optimiser les performances
                        small_current = cv2.resize(gray, (320, 240))
                        small_previous = cv2.resize(previous_frame, (320, 240))
                        
                        # Calculer la différence entre les frames
                        frame_diff = cv2.absdiff(small_current, small_previous)
                        
                        # Appliquer un seuil pour détecter les changements significatifs
                        _, thresh = cv2.threshold(frame_diff, 30, 255, cv2.THRESH_BINARY)
                        
                        # Calculer le pourcentage de pixels qui ont changé
                        motion_pixels = cv2.countNonZero(thresh)
                        total_pixels = small_current.shape[0] * small_current.shape[1]
                        motion_percentage = (motion_pixels / total_pixels) * 100
                        
                        # Si plus de 5% de l'image a changé = mouvement détecté
                        if motion_percentage > 5.0:
                            print("Mouvement détecté")
                            
                            # Créer l'alerte en base de données
                            insert_motion_alert(index + 1)
                            
                            # Mettre à jour le timestamp de la dernière alerte
                            last_motion_alert = current_time
                
                # Sauvegarder la frame actuelle pour la prochaine comparaison
                previous_frame = gray.copy()
                
                time.sleep(3)  # Analyse toutes les 3 secondes
                
            except Exception as e:
                print(f"Erreur détection: {e}")
                time.sleep(5)

    brightness_thread = threading.Thread(target=detect_brightness_and_motion)
    brightness_thread.daemon = True
    brightness_thread.start()

    picam.start_recording(encoder, output)
    print(f"Streaming caméra {index + 1} avec détection de mouvement")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        picam.stop_recording()

def stream_camera_noir(index, output_name, noir_queue):
    picam = Picamera2(index)
    config = picam.create_video_configuration(main={"size": (1920, 1080), "format": "RGB888"})
    picam.configure(config)
    picam.start()

    current_mode = "unknown"

    def adjust_camera():
        nonlocal current_mode
        while True:
            try:
                mode = noir_queue.get(timeout=10)
                
                if mode != current_mode:
                    if mode == "normal":
                        picam.set_controls({
                            "AwbEnable": True,
                            "ExposureTime": 10000,
                            "AnalogueGain": 1.0
                        })
                    else:
                        picam.set_controls({
                            "ExposureTime": 50000,
                            "AnalogueGain": 6.0,
                            "AwbEnable": False,
                            "ColourGains": (1.8, 1.4),
                            "Brightness": 0.3,
                            "Contrast": 1.4,
                            "Saturation": 0.7
                        })
                    
                    current_mode = mode
                    
            except Exception as e:
                time.sleep(1)

    adjust_thread = threading.Thread(target=adjust_camera)
    adjust_thread.daemon = True
    adjust_thread.start()

    encoder = H264Encoder()
    output = FfmpegOutput(f"/mnt/camera/{output_name}", [
        "-f", "hls",
        "-hls_time", "10",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list+split_by_time",
        "-hls_allow_cache", "0",
        "-b:v", "4000k",
        "-maxrate", "5000k",
        "-bufsize", "8000k",
        "-preset", "faster",
        "-tune", "zerolatency"
    ])

    picam.start_recording(encoder, output)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        picam.stop_recording()

if __name__ == "__main__":
    cleanup_old_segments(max_segments=0)
    
    noir_queue = multiprocessing.Queue(maxsize=5)
    
    cam1 = multiprocessing.Process(target=stream_camera_ai, args=(0, "stream.m3u8", noir_queue))
    cam2 = multiprocessing.Process(target=stream_camera_noir, args=(1, "stream2.m3u8", noir_queue))

    cam1.start()
    cam2.start()

    try:
        cam1.join()
        cam2.join()
    except KeyboardInterrupt:
        cam1.terminate()
        cam2.terminate()