from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import json
import base64
import time
import hashlib
from datetime import datetime
import re
import os
from urllib.parse import urlparse
import firebase_admin
from firebase_admin import credentials, auth, firestore
import uuid # Import uuid for session ID generation

app = Flask(__name__)
CORS(app)

# Initialize Firebase
cred = credentials.Certificate("codecompass-efffc-firebase-adminsdk-fbsvc-074a2c539f.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# User limits
MAX_REPOS_PER_DAY =2
MAX_MESSAGES_PER_DAY = 10

def get_user_limits(uid):
    user_ref = db.collection('users').document(uid)
    user_doc = user_ref.get()
    
    today = datetime.now().date()
    
    if user_doc.exists:
        user_data = user_doc.to_dict()
        last_reset_str = user_data.get('last_reset_date')
        last_reset_date = datetime.fromisoformat(last_reset_str).date() if last_reset_str else None
        
        if last_reset_date == today:
            return {
                'repos_processed': user_data.get('repos_processed_today', 0),
                'messages_sent': user_data.get('messages_sent_today', 0)
            }
        else:
            # Reset limits for a new day
            user_ref.update({
                'repos_processed_today': 0,
                'messages_sent_today': 0,
                'last_reset_date': today.isoformat()
            })
            return {
                'repos_processed': 0,
                'messages_sent': 0
            }
    else:
        # New user, initialize limits
        user_ref.set({
            'repos_processed_today': 0,
            'messages_sent_today': 0,
            'last_reset_date': today.isoformat()
        })
        return {
            'repos_processed': 0,
            'messages_sent': 0
        }

def increment_user_limit(uid, limit_type):
    user_ref = db.collection('users').document(uid)
    today = datetime.now().date().isoformat()
    
    if limit_type == 'repo':
        user_ref.update({
            'repos_processed_today': firestore.Increment(1),
            'last_reset_date': today
        })
    elif limit_type == 'message':
        user_ref.update({
            'messages_sent_today': firestore.Increment(1),
            'last_reset_date': today
        })

@app.route('/verify-google-token', methods=['POST'])
def verify_google_token():
    try:
        id_token = request.json.get('id_token')
        if not id_token:
            return jsonify({"error": "ID token is required"}), 400

        # Verify the ID token
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        email = decoded_token['email']
        name = decoded_token.get('name', email)

        # Ensure user profile exists in Firestore and initialize limits if new
        user_ref = db.collection('users').document(uid)
        user_doc = user_ref.get()
        if not user_doc.exists:
            user_ref.set({
                'email': email,
                'name': name,
                'created_at': datetime.now().isoformat(),
                'repos_processed_today': 0,
                'messages_sent_today': 0,
                'last_reset_date': datetime.now().date().isoformat()
            })
        
        # Get current limits for the user
        limits = get_user_limits(uid)

        return jsonify({
            "uid": uid,
            "email": email,
            "name": name,
            "message": "Token verified successfully",
            "limits": {
                "repos_processed_today": limits['repos_processed'],
                "messages_sent_today": limits['messages_sent'],
                "max_repos_per_day": MAX_REPOS_PER_DAY,
                "max_messages_per_day": MAX_MESSAGES_PER_DAY
            }
        }), 200

    except Exception as e:
        print(f"Error verifying token: {e}")
        return jsonify({"error": f"Failed to verify token: {str(e)}"}), 401


# Configuration
OPENROUTER_API_KEY = "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"
GITHUB_API_BASE = "https://api.github.com"

class RepositoryProcessor:
    def __init__(self):
        self.supported_extensions = {
            '.py', '.js', '.jsx', '.ts', '.tsx', '.java', '.cpp', '.c', '.h', '.hpp',
            '.go', '.rs', '.php', '.rb', '.swift', '.kt', '.cs', '.scala', '.clj',
            '.sh', '.bash', '.ps1', '.sql', '.html', '.css', '.scss', '.sass',
            '.vue', '.svelte', '.dart', '.r', '.m', '.pl', '.lua', '.json', '.yaml', '.yml'
        }
        
    def extract_repo_info(self, github_url):
        """Extract owner and repo name from GitHub URL"""
        parsed = urlparse(github_url)
        # Ensure path_parts are clean and correctly split
        path_parts = [part for part in parsed.path.strip('/').split('/') if part]
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1]
            # Remove any trailing .git if present (common in clone URLs)
            if repo.endswith('.git'):
                repo = repo[:-4]
            return owner, repo
        raise ValueError("Invalid GitHub URL format. Expected format: https://github.com/owner/repo")
    
    def generate_repo_id(self, owner, repo, uid):
        """Generate unique repository ID per user"""
        return hashlib.md5(f"{uid}/{owner}/{repo}".encode()).hexdigest()
    
    def get_github_content(self, owner, repo, path="", pat_token=None):
        """Fetch content from GitHub API"""
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}"
        headers = {'Accept': 'application/vnd.github.v3+json'}
        if pat_token:
            headers['Authorization'] = f'token {pat_token}'
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 404:
            print(f"GitHub API 404 Not Found for {url}")
            return None
        else:
            print(f"GitHub API Error: {response.status_code} - {response.text} for URL: {url}")
            response.raise_for_status()
    
    def get_file_content(self, owner, repo, file_path, pat_token=None):
        """Get decoded file content"""
        content_data = self.get_github_content(owner, repo, file_path, pat_token)
        if content_data and content_data.get('content'):
            try:
                decoded_content = base64.b64decode(content_data['content']).decode('utf-8')
                return decoded_content
            except:
                return None
        return None
    
    def should_process_file(self, file_path):
        """Check if file should be processed based on extension"""
        _, ext = os.path.splitext(file_path.lower())
        return ext in self.supported_extensions
    
    def get_repository_structure(self, owner, repo, path="", max_depth=30, current_depth=0, pat_token=None):
        """Recursively get repository structure"""
        if current_depth > max_depth:
            return []
        
        contents = self.get_github_content(owner, repo, path, pat_token)
        if not contents:
            return []
        
        structure = []
        
        for item in contents:
            item_info = {
                'name': item['name'],
                'path': item['path'],
                'type': item['type'],
                'size': item.get('size', 0)
            }
            
            if item['type'] == 'dir':
                item_info['children'] = self.get_repository_structure(
                    owner, repo, item['path'], max_depth, current_depth + 1, pat_token
                )
            elif item['type'] == 'file' and self.should_process_file(item['path']):
                item_info['processable'] = True
            
            structure.append(item_info)
        
        return structure

    def call_llm(self, messages, max_retries=3):
        """Call OpenRouter LLM API with retry logic"""
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    url="https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "http://localhost:5000",
                        "X-Title": "CodeCompass",
                    },
                    data=json.dumps({
                        "model": "deepseek/deepseek-v3-base:free",
                        "messages": messages,
                        "temperature": 0.2,
                        "max_tokens": 2000
                    })
                )
                
                if response.status_code == 200:
                    return response.json()['choices'][0]['message']['content']
                else:
                    print(f"LLM API error (attempt {attempt + 1}): {response.status_code}")
                    if attempt == max_retries - 1:
                        response.raise_for_status()
            except Exception as e:
                print(f"LLM call failed (attempt {attempt + 1}): {str(e)}")
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** attempt)  # Exponential backoff
        
        return "Error: Failed to get LLM response after multiple attempts"

    def analyze_file_metadata(self, file_content, file_path):
        """Extract metadata from file using LLM"""
        prompt = f"""Analyze this code file and extract metadata in JSON format:

File: {file_path}
Content:
```
{file_content[:1000000]}  # Limit content to prevent token overflow
```

Extract and return ONLY a JSON object with these fields:
- functions: List of function/method names
- classes: List of class names
- imports: List of imported modules/libraries
- main_purpose: Brief description of file's purpose
- key_concepts: List of important concepts/patterns used
- dependencies: List of external dependencies used

Return only valid JSON, no other text."""

        try:
            response = self.call_llm([{"role": "user", "content": prompt}])
            # Try to extract JSON from response
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                # Fallback if LLM doesn't return proper JSON
                return {
                    "functions": [],
                    "classes": [],
                    "imports": [],
                    "main_purpose": "Code analysis failed",
                    "key_concepts": [],
                    "dependencies": []
                }
        except Exception as e:
            print(f"Error analyzing file metadata: {str(e)}")
            return {
                "functions": [],
                "classes": [],
                "imports": [],
                "main_purpose": f"Error analyzing {file_path}",
                "key_concepts": [],
                "dependencies": []
            }

    def generate_file_summary(self, file_content, file_path, metadata):
        """Generate summary of file using LLM"""
        prompt = f"""Summarize this code file for developers who are new to the codebase:

File: {file_path}
Metadata: {json.dumps(metadata, indent=2)}
Content:
```
{file_content[:1000000]}
```

Provide a clear, concise summary that includes:
1. What this file does
2. How it fits into the larger application
3. Key functions/classes and their purposes
4. Important dependencies or patterns used
5. Any setup or usage notes for developers

Keep the summary practical and focused on helping new developers understand the code."""

        try:
            return self.call_llm([{"role": "user", "content": prompt}])
        except Exception as e:
            print(f"Error generating file summary: {str(e)}")
            return f"Summary generation failed for {file_path}"

    def generate_common_questions(self, repo_data):
        """Generate common Q&A pairs for the repository"""
        structure_summary = json.dumps(repo_data.get('structure_summary', {}), indent=2)
        files_processed = len(repo_data.get('files', {}))
        
        prompt = f"""Based on this repository analysis, generate common questions developers might ask and their answers:

Repository Structure Summary:
{structure_summary}

Files Processed: {files_processed}

Generate 8-10 practical Q&A pairs that new developers commonly ask about codebases. Format as JSON array:
[
  {{"question": "How do I run this application?", "answer": "Based on the files..."}},
  {{"question": "Where is the main entry point?", "answer": "The main entry point..."}},
  ...
]

Focus on practical questions about:
- Running/starting the application
- Testing procedures
- Main architecture/structure
- Key files and their purposes
- Development setup
- Common workflows

Return only valid JSON array, no other text."""

        try:
            response = self.call_llm([{"role": "user", "content": prompt}])
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return []
        except Exception as e:
            print(f"Error generating common questions: {str(e)}")
            return []

    def analyze_repository_structure(self, structure, owner, repo):
        """Analyze overall repository structure using LLM"""
        structure_text = json.dumps(structure, indent=2)[:1000000]  # Limit size
        
        prompt = f"""Analyze this repository structure and provide insights:

Repository: {owner}/{repo}
Structure:
{structure_text}

Provide analysis in JSON format with these fields:
- architecture_type: Type of application (web app, library, microservice, etc.)
- main_technologies: List of main programming languages/frameworks identified
- project_structure: Description of how the project is organized
- entry_points: Likely main files where execution starts
- build_system: Build tools or package managers detected
- testing_approach: Testing files/frameworks found
- documentation_files: README, docs, or other documentation found
- key_directories: Important directories and their purposes

Return only valid JSON, no other text."""

        try:
            response = self.call_llm([{"role": "user", "content": prompt}])
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return {"error": "Failed to parse structure analysis"}
        except Exception as e:
            print(f"Error analyzing repository structure: {str(e)}")
            return {"error": f"Structure analysis failed: {str(e)}"}

processor = RepositoryProcessor()

def emit_progress(data):
    """Emit progress data as Server-Sent Events"""
    return f"data: {json.dumps(data)}\n\n"

@app.route('/process-repo', methods=['POST'])
def process_repository():
    data = request.get_json()
    github_url = data.get('github_url')
    uid = data.get('uid') # Get UID from request
    is_private = data.get('is_private', False)
    pat_token = data.get('pat_token')

    def generate():
        try:
            if not uid:
                yield emit_progress({"error": "User not authenticated. Please log in."})
                return

            # Check user limits before processing
            user_limits = get_user_limits(uid)
            if user_limits['repos_processed'] >= MAX_REPOS_PER_DAY:
                yield emit_progress({"error": f"Daily repository processing limit ({MAX_REPOS_PER_DAY}) exceeded. Please try again tomorrow."})
                return

            if not github_url:
                yield emit_progress({"error": "GitHub URL is required"})
                return
            
            if is_private and not pat_token:
                yield emit_progress({"error": "Personal Access Token is required for private repositories."})
                return

            # Extract repository information
            try:
                owner, repo = processor.extract_repo_info(github_url)
                repo_id = processor.generate_repo_id(owner, repo, uid)
                yield emit_progress({"progress": 5, "status": "Repository info extracted", "log": f"📁 Processing {owner}/{repo}", "repo_id": repo_id})
            except ValueError as e:
                yield emit_progress({"error": str(e)})
                return
            
            # Check if repository already processed
            repo_doc = db.collection('repositories').document(repo_id).get()
            if repo_doc.exists:
                yield emit_progress({"progress": 100, "status": "Repository already processed", "log": "✅ Repository found in database", "repo_id": repo_id, "complete": True})
                return
            
            # Get repository structure
            yield emit_progress({"progress": 10, "status": "Fetching repository structure", "log": "🔍 Analyzing repository structure..."})
            try:
                structure = processor.get_repository_structure(owner, repo, pat_token=pat_token)
                if not structure:
                    yield emit_progress({"error": "Repository not found or is empty. Check URL and PAT if private."})
                    return
                yield emit_progress({"progress": 20, "status": "Repository structure fetched", "log": f"📊 Found {len(structure)} top-level items"})
            except Exception as e:
                yield emit_progress({"error": f"Failed to fetch repository structure: {str(e)}. Ensure the URL is correct and PAT is valid for private repos."})
                return
            
            # Analyze overall structure
            yield emit_progress({"progress": 25, "status": "Analyzing repository architecture", "log": "🏗️ Analyzing overall architecture..."})
            structure_summary = processor.analyze_repository_structure(structure, owner, repo)
            
            # Collect processable files
            yield emit_progress({"progress": 30, "status": "Identifying code files", "log": "📁 Collecting code files for analysis..."})
            processable_files = []
            
            def collect_files(items, path_prefix=""):
                for item in items:
                    if item['type'] == 'file' and item.get('processable'):
                        processable_files.append(item['path'])
                    elif item['type'] == 'dir' and 'children' in item:
                        collect_files(item['children'], item['path'] + "/")
            
            collect_files(structure)
            
            # Removed the 50-file limit to allow processing of all identified files.
            # This ensures that all relevant files are available for AI context.
            yield emit_progress({"progress": 40, "status": f"Processing {len(processable_files)} files", "log": f"🔄 Starting analysis of {len(processable_files)} code files..."})
            
            # Process files
            processed_files = {}
            for i, file_path in enumerate(processable_files):
                try:
                    progress = 40 + (i / len(processable_files)) * 40  # 40-80% for file processing
                    yield emit_progress({"progress": progress, "status": f"Processing {file_path}", "log": f"📄 Analyzing {file_path}..."})
                    
                    # Get file content
                    file_content = processor.get_file_content(owner, repo, file_path, pat_token)
                    if not file_content:
                        continue
                    
                    # Analyze metadata
                    metadata = processor.analyze_file_metadata(file_content, file_path)
                    
                    # Generate summary
                    summary = processor.generate_file_summary(file_content, file_path, metadata)
                    
                    processed_files[file_path] = {
                        'content': file_content[:1000000],  # Store first 1000000 chars
                        'metadata': metadata,
                        'summary': summary,
                        'size': len(file_content),
                        'processed_at': datetime.now().isoformat()
                    }
                    
                    # Brief pause to prevent rate limiting
                    time.sleep(0.1)
                    
                except Exception as e:
                    yield emit_progress({"log": f"❌ Error processing {file_path}: {str(e)}"})
                    continue
            
            yield emit_progress({"progress": 80, "status": "Generating documentation", "log": "📚 Generating common questions and documentation..."})
            
            # Prepare repository data
            repo_data = {
                'repo_id': repo_id,
                'owner': owner,
                'repo': repo,
                'github_url': github_url,
                'is_private': is_private, # Store if it's a private repo
                'structure': structure,
                'structure_summary': structure_summary,
                'files': processed_files,
                'processed_at': datetime.now().isoformat(),
                'total_files': len(processable_files),
                'processed_files': len(processed_files),
                'processed_by_uid': uid # Store UID of the user who processed it
            }
            
            # Generate common Q&A
            common_qa = processor.generate_common_questions(repo_data)
            repo_data['common_qa'] = common_qa
            
            yield emit_progress({"progress": 90, "status": "Saving to database", "log": "💾 Saving processed data to database..."})
            
            # Save to Firestore
            try:
                db.collection('repositories').document(repo_id).set(repo_data)
                increment_user_limit(uid, 'repo') # Increment repo limit for the user
                yield emit_progress({"progress": 100, "status": "Processing complete!", "log": "✅ Repository successfully processed and saved!", "repo_id": repo_id, "complete": True})
            except Exception as e:
                yield emit_progress({"error": f"Failed to save to database: {str(e)}"})
                return
            
        except Exception as e:
            yield emit_progress({"error": f"Unexpected error: {str(e)}"})
    
    return Response(generate(), mimetype='text/plain')

@app.route('/ask-question', methods=['POST'])
def ask_question():
    try:
        data = request.get_json()
        repo_id = data.get('repo_id')
        question = data.get('question')
        session_id = data.get('session_id')
        uid = data.get('uid') # Get UID from request
        
        if not uid:
            return jsonify({"error": "User not authenticated. Please log in."}), 401

        # Check user limits before answering
        user_limits = get_user_limits(uid)
        if user_limits['messages_sent'] >= MAX_MESSAGES_PER_DAY:
            return jsonify({"error": f"Daily message limit ({MAX_MESSAGES_PER_DAY}) exceeded. Please try again tomorrow."}), 429
        
        if not repo_id or not question:
            return jsonify({"error": "Repository ID and question are required"}), 400
        
        # Generate a new session ID if not provided
        if not session_id:
            session_id = str(uuid.uuid4())
            
        # Get repository data from Firestore
        repo_doc = db.collection('repositories').document(repo_id).get()
        if not repo_doc.exists:
            return jsonify({"error": "Repository not found"}), 404
        
        repo_data = repo_doc.to_dict()
        
        # Fetch chat history for the session under the user's collection
        chat_history_ref = db.collection('users').document(uid).collection('chats').document(session_id)
        chat_history_doc = chat_history_ref.get()
        
        current_chat_history = []
        if chat_history_doc.exists:
            current_chat_history = chat_history_doc.to_dict().get('history', [])
        
        # Check if question is very similar to common Q&A (more flexible matching)
        common_qa = repo_data.get('common_qa', [])
        question_words = set(question.lower().split())
        
        for qa in common_qa:
            qa_words = set(qa['question'].lower().split())
            # Check for significant word overlap
            overlap = len(question_words.intersection(qa_words))
            if overlap >= 2 and overlap / len(question_words.union(qa_words)) > 0.3:
                # Save the current Q&A to history before returning
                # For common_qa, relevant_context is not explicitly generated, so we pass an empty list
                new_qa_pair = {"question": question, "answer": qa['answer'], "timestamp": datetime.now().isoformat(), "context_files": []}
                current_chat_history.append(new_qa_pair)
                # Keep only the last 10 pairs
                current_chat_history = current_chat_history[-10:]
                chat_history_ref.set({'history': current_chat_history, 'repo_id': repo_id}) # Store repo_id with chat history

                increment_user_limit(uid, 'message') # Increment message limit
                return jsonify({
                    "answer": qa['answer'], 
                    "source": "common_qa",
                    "matched_question": qa['question'],
                    "session_id": session_id # Return session ID
                })
        
        # Use AI-guided search for relevant content
        relevant_context = search_relevant_content(repo_data, question)
        
        # Generate answer with enhanced context, including chat history
        answer = generate_answer_with_context(question, relevant_context, repo_data, current_chat_history)
        
        # Save the new Q&A pair to history, including the relevant files
        new_qa_pair = {"question": question, "answer": answer, "timestamp": datetime.now().isoformat(), "context_files": relevant_context}
        current_chat_history.append(new_qa_pair)
        # Keep only the last 10 pairs
        current_chat_history = current_chat_history[-10:]
        chat_history_ref.set({'history': current_chat_history, 'repo_id': repo_id}) # Save to Firestore, store repo_id

        increment_user_limit(uid, 'message') # Increment message limit
        return jsonify({
            "answer": answer, 
            "source": "ai_analysis",
            "files_analyzed": len(relevant_context),
            "analysis_summary": [
                {"file": item['path'], "reason": item.get('reason', '')} 
                for item in relevant_context
            ],
            "session_id": session_id # Return session ID
        })
        
    except Exception as e:
        print(f"Error in ask_question: {str(e)}")
        return jsonify({"error": f"An error occurred while processing your question: {str(e)}"}), 500

@app.route('/get-user-processed-repos', methods=['GET'])
def get_user_processed_repos():
    try:
        uid = request.args.get('uid')
        if not uid:
            return jsonify({"error": "User ID (uid) is required"}), 400

        repos_ref = db.collection('repositories')
        query = repos_ref.where('processed_by_uid', '==', uid)
        
        processed_repos = []
        for doc in query.stream():
            repo_data = doc.to_dict()
            processed_repos.append({
                'repo_id': doc.id,
                'owner': repo_data.get('owner'),
                'repo': repo_data.get('repo'),
                'github_url': repo_data.get('github_url'),
                'processed_at': repo_data.get('processed_at')
            })
        
        # Sort by processed_at, newest first
        processed_repos.sort(key=lambda x: x['processed_at'] or '', reverse=True)

        return jsonify({"repos": processed_repos}), 200

    except Exception as e:
        print(f"Error in get_user_processed_repos: {str(e)}")
        return jsonify({"error": f"An error occurred while fetching processed repositories: {str(e)}"}), 500

@app.route('/get-user-chat-sessions', methods=['GET'])
def get_user_chat_sessions():
    try:
        uid = request.args.get('uid')
        repo_id = request.args.get('repo_id')

        if not uid:
            return jsonify({"error": "User ID (uid) is required"}), 400

        chats_ref = db.collection('users').document(uid).collection('chats')
        
        query = chats_ref
        if repo_id:
            query = query.where('repo_id', '==', repo_id)
        
        sessions = []
        for doc in query.stream():
            session_data = doc.to_dict()
            # Only return essential info for listing sessions
            sessions.append({
                'session_id': doc.id,
                'repo_id': session_data.get('repo_id'),
                'last_message_timestamp': session_data.get('history', [{}])[-1].get('timestamp') if session_data.get('history') else None,
                'first_question': session_data.get('history', [{}])[0].get('question') if session_data.get('history') else None
            })
        
        # Sort sessions by last message timestamp, newest first
        sessions.sort(key=lambda x: x['last_message_timestamp'] or '', reverse=True)

        return jsonify({"sessions": sessions}), 200

    except Exception as e:
        print(f"Error in get_user_chat_sessions: {str(e)}")
        return jsonify({"error": f"An error occurred while fetching chat sessions: {str(e)}"}), 500

@app.route('/get-chat-history', methods=['GET'])
def get_chat_history():
    try:
        uid = request.args.get('uid')
        session_id = request.args.get('session_id')

        if not uid or not session_id:
            return jsonify({"error": "User ID (uid) and Session ID (session_id) are required"}), 400

        chat_history_ref = db.collection('users').document(uid).collection('chats').document(session_id)
        chat_history_doc = chat_history_ref.get()

        if not chat_history_doc.exists:
            return jsonify({"error": "Chat session not found"}), 404
        
        return jsonify(chat_history_doc.to_dict()), 200

    except Exception as e:
        print(f"Error in get_chat_history: {str(e)}")
        return jsonify({"error": f"An error occurred while fetching chat history: {str(e)}"}), 500

def search_relevant_content(repo_data, question):
    """Direct content search that uses Firebase data effectively"""
    
    files = repo_data.get('files', {})
    if not files:
        return []
    
    # Get repository context
    structure_summary = repo_data.get('structure_summary', {})
    repo_context = f"""Repository: {repo_data.get('owner')}/{repo_data.get('repo')}
Architecture: {structure_summary.get('architecture_type', 'Unknown')}
Technologies: {', '.join(structure_summary.get('main_technologies', []))}
Entry Points: {', '.join(structure_summary.get('entry_points', []))}"""
    
    # Create file analysis for AI
    file_summaries = []
    for file_path, file_data in files.items():
        file_info = {
            'path': file_path,
            'purpose': file_data.get('metadata', {}).get('main_purpose', 'Unknown'),
            'summary': file_data.get('summary', '')[:1000000],  # Truncate for context
            'functions': file_data.get('metadata', {}).get('functions', [])[:5],
            'classes': file_data.get('metadata', {}).get('classes', [])[:5],
            'content_preview': file_data.get('content', '')[:300]  # First 300 chars
        }
        file_summaries.append(file_info)
    
    # Let AI select relevant files
# Let AI select relevant files
    newline = chr(10)
    file_analysis = newline.join([f"File: {f['path']}\nPurpose: {f['purpose']}\nSummary: {f['summary']}\nFunctions: {', '.join(f['functions'])}\nClasses: {', '.join(f['classes'])}\nPreview: {f['content_preview'][:1000000]}...\n---" for f in file_summaries[:15]])

    selection_prompt = f"""{repo_context}

Available Files Analysis:
{file_analysis}

User Question: {question}

Based on the question and file analysis above, select the 3-5 most relevant files that would help answer this question. 

Respond with only a JSON array of file paths like: ["file1.py", "file2.js", "file3.html"]

Consider:
- Files whose purpose/summary relates to the question
- Files that contain functions/classes mentioned in the question
- Entry point files for setup/running questions
- Configuration files for setup questions
- Main application files for architecture questions"""

    try:
        response = processor.call_llm([{"role": "user", "content": selection_prompt}])
        print(f"AI Selection Response: {response}")  # Debug log
        
        # Extract JSON array from response
        import re
        json_match = re.search(r'\[.*?\]', response, re.DOTALL)
        if json_match:
            selected_files = json.loads(json_match.group())
            print(f"Selected files: {selected_files}")  # Debug log
        else:
            # Fallback: select files based on keywords
            question_lower = question.lower()
            selected_files = []
            for file_path in files.keys():
                if any(keyword in file_path.lower() or keyword in files[file_path].get('summary', '').lower() 
                       for keyword in ['main', 'index', 'app', 'config', 'setup']):
                    selected_files.append(file_path)
                    if len(selected_files) >= 5:
                        break
        
        # Return full file data for selected files
        relevant_context = []
        for file_path in selected_files:
            if file_path in files:
                file_data = files[file_path]
                relevant_context.append({
                    'path': file_path,
                    'summary': file_data.get('summary', ''),
                    'content': file_data.get('content', ''),
                    'metadata': file_data.get('metadata', {}),
                    'type': 'full_analysis',
                    'reason': 'Selected by AI as relevant to the question'
                })
        
        print(f"Returning {len(relevant_context)} files with full data")  # Debug log
        return relevant_context
        
    except Exception as e:
        print(f"Error in file selection: {str(e)}")
        # Fallback: return first few files
        fallback_files = list(files.keys())[:3]
        return [{
            'path': file_path,
            'summary': files[file_path].get('summary', ''),
            'content': files[file_path].get('content', ''),
            'metadata': files[file_path].get('metadata', {}),
            'type': 'fallback',
            'reason': 'Fallback selection due to AI selection error'
        } for file_path in fallback_files]

def generate_answer_with_context(question, relevant_context, repo_data, chat_history):
    """Generate answer using LLM with relevant context and chat history"""
    
    # Build context string
    context_parts = []
    
    # Add repository overview
    structure_summary = repo_data.get('structure_summary', {})
    context_parts.append(f"Repository: {repo_data.get('owner')}/{repo_data.get('repo')}")
    context_parts.append(f"Architecture: {structure_summary.get('architecture_type', 'Unknown')}")
    context_parts.append(f"Technologies: {', '.join(structure_summary.get('main_technologies', []))}")
    
    # Add chat history, including context files from previous turns
    if chat_history:
        context_parts.append("\nPrevious Conversation History (last 10 Q&A pairs):")
        for i, qa in enumerate(chat_history):
            context_parts.append(f"Q{i+1}: {qa['question']}")
            context_parts.append(f"A{i+1}: {qa['answer']}")
            if qa.get('context_files'):
                context_parts.append("Files referenced in Q&A:")
                for file_item in qa['context_files']:
                    context_parts.append(f"  - {file_item['path']}")
                    if file_item.get('summary'):
                        context_parts.append(f"    Summary: {file_item['summary'][:1000000]}...")
                    if file_item.get('content'):
                        context_parts.append(f"    Code snippet:\n```\n{file_item['content'][:1000000]}...\n```")
    
    # Add relevant file information for the current turn
    if relevant_context:
        context_parts.append("\nRelevant Files for Current Question:")
        for item in relevant_context:
            context_parts.append(f"\nFile: {item['path']}")
            context_parts.append(f"Purpose: {item.get('metadata', {}).get('main_purpose', 'N/A')}")
            context_parts.append(f"Summary: {item.get('summary', 'N/A')[:1000000]}...")
            
            # Add code snippet if relevant
            if item.get('content'):
                context_parts.append(f"Code snippet:\n```\n{item['content'][:1000000]}...\n```")
    
    context = "\n".join(context_parts)
    
    # Create prompt for LLM
    prompt = f"""You are CodeCompass, an AI assistant helping developers understand a codebase. Answer the user's question based on the repository context and previous conversation history provided.

Repository Context:
{context}

User Question: {question}

Provide a helpful, detailed answer that:
1. Directly addresses the user's question
2. References specific files or code when relevant
3. Includes practical steps or commands when applicable
4. Uses the actual repository structure and content in your response
5. Is clear and actionable for a developer
6. Takes into account the previous conversation to maintain context and avoid redundancy.

If you cannot find specific information to answer the question, say so and suggest what the user might look for or where they might find the answer."""

    try:
        response = processor.call_llm([{"role": "user", "content": prompt}])
        return response
    except Exception as e:
        return f"I apologize, but I encountered an error generating an answer: {str(e)}. Please try rephrasing your question or ask about something more specific."

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})

if __name__ == '__main__':
    print("🧭 CodeCompass Backend Starting...")
    print("🔥 Firebase initialized")
    print("🤖 OpenRouter LLM ready")
    print("🚀 Server running on http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=8080)
