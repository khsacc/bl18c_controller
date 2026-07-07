import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, filedialog
import math
import datetime
import os
import shutil
import time
from collections import deque

def nothing(x):
    pass

def setup_window(window_name, callback, cross_val, circle_val, radius_val):
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.createTrackbar('Cross: 0=Off, 1=On', window_name, cross_val, 1, nothing)
    cv2.createTrackbar('Circle: 0=Off, 1=On', window_name, circle_val, 1, nothing)
    cv2.createTrackbar('Radius', window_name, radius_val, 200, nothing)
    cv2.setMouseCallback(window_name, callback)

def main():
    root = tk.Tk()
    root.withdraw()

    window_name = 'USB Camera'
    cap = cv2.VideoCapture(0)

    if not cap.isOpened():
        print("Error: Could not open camera.")
        return

    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    default_fps = cap.get(cv2.CAP_PROP_FPS)
    if default_fps == 0.0 or default_fps == -1.0:
        default_fps = 30.0

    marks = []
    dragging_idx = -1
    selected_idx = -1
    drag_threshold = 20
    is_recording = False
    video_writer = None
    temp_video_path = "temp_recording.mp4"
    
    show_all_marks = True  # 全マークの表示/非表示を管理するフラグ

    render_params = {'scale': 1.0, 'dx': 0, 'dy': 0}
    frame_times = deque(maxlen=30)

    def mouse_events(event, x, y, flags, param):
        nonlocal dragging_idx, selected_idx, show_all_marks

        scale = render_params['scale']
        dx = render_params['dx']
        dy = render_params['dy']

        if scale <= 0:
            scale = 1.0

        fx = int((x - dx) / scale)
        fy = int((y - dy) / scale)

        if fx < 0 or fx >= frame_width or fy < 0 or fy >= frame_height:
            if event == cv2.EVENT_LBUTTONUP:
                dragging_idx = -1
            return

        # 非表示の状態でクリックされたら、誤操作防止のため自動的に再表示する
        if not show_all_marks and (event == cv2.EVENT_LBUTTONDOWN or event == cv2.EVENT_RBUTTONDOWN):
            show_all_marks = True

        if event == cv2.EVENT_LBUTTONDOWN:
            clicked_existing = False
            for i, m in enumerate(marks):
                if math.hypot(fx - m['x'], fy - m['y']) <= drag_threshold:
                    dragging_idx = i
                    selected_idx = i
                    try:
                        cv2.setTrackbarPos('Cross: 0=Off, 1=On', window_name, m['cross'])
                        cv2.setTrackbarPos('Circle: 0=Off, 1=On', window_name, m['circle'])
                        cv2.setTrackbarPos('Radius', window_name, m['r'])
                    except cv2.error:
                        pass
                    clicked_existing = True
                    break
            
            if not clicked_existing:
                try:
                    c_cross = cv2.getTrackbarPos('Cross: 0=Off, 1=On', window_name)
                    c_circle = cv2.getTrackbarPos('Circle: 0=Off, 1=On', window_name)
                    c_r = cv2.getTrackbarPos('Radius', window_name)
                except cv2.error:
                    c_cross, c_circle, c_r = 1, 1, 15
                
                if c_r < 1: c_r = 1
                
                marks.append({'x': fx, 'y': fy, 'cross': c_cross, 'circle': c_circle, 'r': c_r})
                dragging_idx = len(marks) - 1
                selected_idx = len(marks) - 1

        elif event == cv2.EVENT_MOUSEMOVE:
            if dragging_idx != -1:
                marks[dragging_idx]['x'] = fx
                marks[dragging_idx]['y'] = fy

        elif event == cv2.EVENT_LBUTTONUP:
            dragging_idx = -1

        elif event == cv2.EVENT_RBUTTONDOWN:
            if marks:
                marks.pop()
                if dragging_idx >= len(marks):
                    dragging_idx = -1
                if selected_idx >= len(marks):
                    selected_idx = len(marks) - 1
                    if selected_idx != -1:
                        m = marks[selected_idx]
                        try:
                            cv2.setTrackbarPos('Cross: 0=Off, 1=On', window_name, m['cross'])
                            cv2.setTrackbarPos('Circle: 0=Off, 1=On', window_name, m['circle'])
                            cv2.setTrackbarPos('Radius', window_name, m['r'])
                        except cv2.error:
                            pass

    cross_val = 1
    circle_val = 1
    radius_val = 15

    setup_window(window_name, mouse_events, cross_val, circle_val, radius_val)

    color_white = (255, 255, 255)
    color_yellow = (0, 255, 255)
    thickness = 1

    while True:
        ret, frame = cap.read()

        if not ret:
            print("Error: Could not read frame.")
            break
            
        frame_times.append(time.time())

        try:
            cur_cross = cv2.getTrackbarPos('Cross: 0=Off, 1=On', window_name)
            cur_circle = cv2.getTrackbarPos('Circle: 0=Off, 1=On', window_name)
            cur_r = cv2.getTrackbarPos('Radius', window_name)
            if cur_r < 1: cur_r = 1
            
            if selected_idx != -1 and selected_idx < len(marks):
                marks[selected_idx]['cross'] = cur_cross
                marks[selected_idx]['circle'] = cur_circle
                marks[selected_idx]['r'] = cur_r
        except cv2.error:
            pass

        # show_all_marksがTrueの時だけ画像にマークを描画する
        if show_all_marks:
            for i, m in enumerate(marks):
                line_length = 20
                mx, my = m['x'], m['y']
                
                current_color = color_yellow if i == selected_idx else color_white
                
                if m['cross'] == 1:
                    cv2.line(frame, (mx - line_length, my), (mx + line_length, my), current_color, thickness)
                    cv2.line(frame, (mx, my - line_length), (mx, my + line_length), current_color, thickness)
                
                if m['circle'] == 1:
                    cv2.circle(frame, (mx, my), m['r'], current_color, thickness)

        display_frame = frame.copy()

        if is_recording:
            video_writer.write(frame)
            cv2.circle(display_frame, (30, 30), 10, (0, 0, 255), -1)
            cv2.putText(display_frame, "REC", (50, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        try:
            rect = cv2.getWindowImageRect(window_name)
            win_w, win_h = rect[2], rect[3]
        except cv2.error:
            win_w, win_h = 0, 0

        if win_w > 0 and win_h > 0:
            text_area_h = 30
            avail_h = max(1, win_h - text_area_h)
            
            scale = min(win_w / frame_width, avail_h / frame_height)
            new_w = int(frame_width * scale)
            new_h = int(frame_height * scale)
            
            dx = (win_w - new_w) // 2
            dy = text_area_h + (avail_h - new_h) // 2

            render_params['scale'] = scale
            render_params['dx'] = dx
            render_params['dy'] = dy

            resized_frame = cv2.resize(display_frame, (new_w, new_h))
            canvas = np.zeros((win_h, win_w, 3), dtype=np.uint8)
            
            canvas[dy:dy+new_h, dx:dx+new_w] = resized_frame

            cv2.rectangle(canvas, (dx - 1, dy - 1), (dx + new_w, dy + new_h), (255, 255, 255), 1)

            instruction_text = "Keys: [Esc]Exit [S]Save [V]Video [Q]Marks Show/Hide [X] Cross Show/Hide [Y] Circle Show/Hide"
            cv2.putText(canvas, instruction_text, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

            try:
                cv2.imshow(window_name, canvas)
            except cv2.error:
                pass
        else:
            render_params['scale'] = 1.0
            render_params['dx'] = 0
            render_params['dy'] = 0
            try:
                cv2.imshow(window_name, display_frame)
            except cv2.error:
                pass

        key = cv2.waitKey(1) & 0xFF
        
        if key == 27:  # 27 = ESC key
            if messagebox.askyesno("Confirm Exit", "Are you sure you want to exit?"):
                break

        elif key == ord('q'):
            show_all_marks = not show_all_marks
                
        elif key == ord('c'):
            marks.clear()
            selected_idx = -1

        elif key == ord('x'):
            try:
                val = cv2.getTrackbarPos('Cross: 0=Off, 1=On', window_name)
                new_val = 0 if val == 1 else 1
                cv2.setTrackbarPos('Cross: 0=Off, 1=On', window_name, new_val)
            except cv2.error:
                pass

        elif key == ord('z'):
            try:
                val = cv2.getTrackbarPos('Circle: 0=Off, 1=On', window_name)
                new_val = 0 if val == 1 else 1
                cv2.setTrackbarPos('Circle: 0=Off, 1=On', window_name, new_val)
            except cv2.error:
                pass
            
        elif key == ord('s'):
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            default_filename = f"snapshot_{timestamp}.jpg"
            file_path = filedialog.asksaveasfilename(
                title="Save Snapshot",
                initialfile=default_filename,
                defaultextension=".jpg",
                filetypes=[("JPEG files", "*.jpg"), ("All files", "*.*")]
            )
            if file_path:
                cv2.imwrite(file_path, frame)
                print(f"Saved snapshot: {file_path}")
                
        elif key == ord('v'):
            if not is_recording:
                if len(frame_times) > 1:
                    duration = frame_times[-1] - frame_times[0]
                    if duration > 0:
                        measured_fps = (len(frame_times) - 1) / duration
                    else:
                        measured_fps = default_fps
                else:
                    measured_fps = default_fps
                
                measured_fps = max(1.0, min(measured_fps, 120.0))
                
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                video_writer = cv2.VideoWriter(temp_video_path, fourcc, measured_fps, (frame_width, frame_height))
                is_recording = True
                print(f"Started recording at {measured_fps:.2f} FPS...")
            else:
                is_recording = False
                if video_writer is not None:
                    video_writer.release()
                    video_writer = None
                
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                default_filename = f"video_{timestamp}.mp4"
                file_path = filedialog.asksaveasfilename(
                    title="Save Video",
                    initialfile=default_filename,
                    defaultextension=".mp4",
                    filetypes=[("MP4 files", "*.mp4"), ("All files", "*.*")]
                )
                
                if file_path and os.path.exists(temp_video_path):
                    shutil.move(temp_video_path, file_path)
                    print(f"Saved video: {file_path}")
                else:
                    if os.path.exists(temp_video_path):
                        os.remove(temp_video_path)
                    print("Recording discarded.")

        try:
            is_visible = cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE)
        except cv2.error:
            is_visible = 0

        if is_visible < 1:
            if messagebox.askyesno("Confirm Exit", "Are you sure you want to exit?"):
                break
            else:
                setup_window(window_name, mouse_events, cross_val, circle_val, radius_val)

    if video_writer is not None:
        video_writer.release()
    if os.path.exists(temp_video_path):
        os.remove(temp_video_path)
        
    cap.release()
    cv2.destroyAllWindows()
    root.destroy()

if __name__ == "__main__":
    main()