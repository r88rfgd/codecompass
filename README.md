# CodeCompass 🧭 - AI-Powered Code Onboarding & Knowledge Agent

CodeCompass is a web app that helps developers quickly understand unfamiliar codebases using AI. It processes GitHub repositories and allows you to ask natural language questions about the code's structure, setup, and logic.

---

## ✨ Features

- Ask AI questions about GitHub repositories  
- Google Sign-In via Firebase  
- Chat session history per user and repo  
- Usage limits  
- Clean, modern frontend with dark theme  

---


## 📹 Demo Video



https://github.com/user-attachments/assets/110d9f36-a7e0-45fc-a55c-1bf1aa65dc65



*Note: I compressed the video quite a bit to keep the file size small — sorry about the quality! But I assure you, the UI looks great in actual usage.*

---

## 🛠 Tech Stack

- **Frontend**: HTML + JavaScript + Firebase  
- **Backend**: Python (Flask)  
- **Database/Auth**: Firebase Firestore + Firebase Authentication  
- **AI**: OpenRouter API (`deepseek-v3-base:free`)  

---

## 🔧 Setup Instructions

### 1. Clone This Repo

```bash
git clone https://github.com/your-username/codecompass.git
cd codecompass
```

### 2. Install Backend Dependencies

```bash
pip install flask flask-cors requests firebase-admin google-cloud-firestore uuid
```

### 3. Firebase Setup

1. Go to [Firebase Console](https://console.firebase.google.com/) and create a project.  
2. Enable **Google Sign-In** under *Authentication → Sign-in method*.  
3. Enable **Firestore Database**.  
4. Create a **Web App** and get the Firebase config values.  
5. Generate a **Service Account Key**, download the JSON file, and rename it to:

```
codecompass-efffc-firebase-adminsdk-fbsvc-074a2c539f.json
```

Put this file in the same folder as `app.py`.

---

### 4. Update Firebase Config in `index.html`

Open `index.html` and replace the Firebase config section with your actual values:

```js
const firebaseConfig = {
    apiKey: "your-api-key",                // 🔑 from Firebase Web App
    authDomain: "your-auth-domain",        // e.g., yourapp.firebaseapp.com
    projectId: "your-project-id",
    storageBucket: "your-storage-bucket",
    messagingSenderId: "your-sender-id",
    appId: "your-app-id"
};
```

---

### 5. Add Your OpenRouter API Key

Open `app.py` and replace this line:

```python
OPENROUTER_API_KEY = "your_openrouter_api_key"
```

With your actual OpenRouter API key (get it from [openrouter.ai](https://openrouter.ai/)).

---

### 6. Run the Server

```bash
python app.py
```

---

### 7. Open the Frontend

Open the `index.html` file in any browser.

---

## 📡 API Overview

| Endpoint                    | Method | Description                         |
|----------------------------|--------|-------------------------------------|
| `/verify-google-token`     | POST   | Verifies Firebase auth token        |
| `/process-repo`            | POST   | Clones and processes a GitHub repo  |
| `/ask-question`            | POST   | Submits a question about the repo   |
| `/get-user-processed-repos`| GET    | Lists repos user has processed      |
| `/get-user-chat-sessions`  | GET    | Lists chat sessions by repo         |
| `/get-chat-history`        | GET    | Loads previous chat history         |
| `/health`                  | GET    | Health check for backend            |

---

## 🧠 Troubleshooting

- **CORS errors?** Make sure `Flask-CORS` is imported and `CORS(app)` is in `app.py`.  
- **Blank screen?** Check browser console for Firebase config issues.  
- **Repo fails to process?** Ensure the GitHub URL is public or use a PAT.  
