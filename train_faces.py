import cv2
import os
import numpy as np
import pickle

FACE_FOLDER = os.path.join('static', 'faces')

def train_model():
    recognizer = cv2.face.LBPHFaceRecognizer_create()

    faces = []
    labels = []
    label_to_username = {}
    current_label = 0

    for username in os.listdir(FACE_FOLDER):
        user_folder = os.path.join(FACE_FOLDER, username)
        if not os.path.isdir(user_folder):
            continue

        label_to_username[current_label] = username

        for filename in os.listdir(user_folder):
            if filename.endswith('.jpg'):
                filepath = os.path.join(user_folder, filename)
                image = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
                faces.append(image)
                labels.append(current_label)

        current_label += 1

    if len(faces) == 0:
        print("No enrolled faces found. Enroll at least one staff member first.")
        return

    recognizer.train(faces, np.array(labels))
    recognizer.save('face_model.yml')

    with open('label_map.pkl', 'wb') as f:
        pickle.dump(label_to_username, f)

    print(f"Training complete. Trained on {len(faces)} photos across {len(label_to_username)} people.")
    print("People:", list(label_to_username.values()))

if __name__ == '__main__':
    train_model()