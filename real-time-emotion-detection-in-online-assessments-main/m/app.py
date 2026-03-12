from collections import Counter
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
from bson import ObjectId
import subprocess
import sys
import functools
import os

app = Flask(__name__)
app.secret_key = 'super_secret_key'

# MongoDB setup
client = MongoClient('mongodb://localhost:27017/')
db = client['emvi_portal']

assignments_collection = db['assignments']
answers_collection = db['student_answers']
emotion_collection = db['emotion_logs']
students_collection = db['students']
teachers_collection = db['teachers']
admins_collection = db['admins']

student_todos = {}
active_model_processes = {}


def login_required(role=None):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if 'username' not in session or 'role' not in session:
                return redirect(url_for('home'))
            if role and session['role'] != role:
                return redirect(url_for('home'))
            return func(*args, **kwargs)
        return wrapper
    return decorator


def get_user_collection(role):
    return {
        'student': students_collection,
        'teacher': teachers_collection,
        'admin': admins_collection
    }.get(role)


@app.route('/')
def home():
    if 'username' in session and 'role' in session:
        role = session['role']
        username = session['username']
        if role == 'student':
            return redirect(url_for('student_dashboard', username=username))
        elif role == 'teacher':
            return redirect(url_for('teacher_dashboard', username=username))
        elif role == 'admin':
            return redirect(url_for('admin_dashboard', username=username))
    return render_template('login.html')


@app.route('/direct-admin')
def direct_admin():
    return redirect(url_for('admin_login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        role = request.form.get('role', '').lower()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        rollno = request.form.get('rollno', '').strip() if role == 'student' else None
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        # only student and teacher can register
        if role not in ['student', 'teacher']:
            return "Invalid role. <a href='/register'>Try again</a>"
        if not username or not email or not password or not confirm_password:
            return "All fields are required. <a href='/register'>Try again</a>"
        if password != confirm_password:
            return "Passwords do not match. <a href='/register'>Try again</a>"

        collection = get_user_collection(role)

        if collection.find_one({'username': username}):
            return "Username already exists. <a href='/register'>Try again</a>"
        if collection.find_one({'email': email}):
            return "Email already registered. <a href='/register'>Try again</a>"

        if role == 'student':
            if not rollno:
                return "Roll number is required for students. <a href='/register'>Try again</a>"
            if collection.find_one({'rollno': rollno}):
                return "Roll number already registered. <a href='/register'>Try again</a>"

        user_data = {
            'username': username,
            'email': email,
            'password_hash': generate_password_hash(password),
        }

        if role == 'student':
            user_data['rollno'] = rollno

        collection.insert_one(user_data)
        return redirect(url_for('home'))

    return render_template('register.html')


@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if username == 'gretta' and password == 'klgretta':
            session['username'] = 'gretta'
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard', username='gretta'))
        else:
            return "Invalid admin credentials. <a href='/admin-login'>Try again</a>"

    return render_template('admin_login.html')


@app.route('/login', methods=['POST'])
def login():
    role = request.form.get('role', '').strip().lower()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    collection = get_user_collection(role)

    if collection is None:
        return "Invalid credentials. <a href='/'>Try again</a>"

    user = collection.find_one({'username': username})

    if user and user.get('password_hash') and check_password_hash(user['password_hash'], password):
        session['username'] = username
        session['role'] = role

        if role == 'student':
            student_todos.setdefault(username, [])
            return redirect(url_for('student_dashboard', username=username))
        elif role == 'teacher':
            return redirect(url_for('teacher_dashboard', username=username))

    return "Invalid credentials. <a href='/'>Try again</a>"


@app.route('/admin_dashboard/<username>')
@login_required(role='admin')
def admin_dashboard(username):
    grouped_users = {
        'student': list(students_collection.find()),
        'teacher': list(teachers_collection.find())
    }

    assignments = list(assignments_collection.find())
    results = []

    for r in answers_collection.find():
        assignment = assignments_collection.find_one({'_id': ObjectId(r['assignment_id'])})
        results.append({
            'student': r['student'],
            'score': r['score'],
            'assignment_title': assignment['title'] if assignment else 'Unknown'
        })

    return render_template(
        'admin_dashboard.html',
        name=username,
        users=grouped_users,
        assignments=assignments,
        results=results
    )


@app.route('/delete-assignment/<assignment_id>', methods=['POST'])
@login_required(role='admin')
def delete_assignment(assignment_id):
    assignments_collection.delete_one({'_id': ObjectId(assignment_id)})
    answers_collection.delete_many({'assignment_id': assignment_id})
    return redirect(url_for('admin_dashboard', username=session['username']))


@app.route('/delete-user/<role>/<username>', methods=['POST'])
@login_required(role='admin')
def delete_user(role, username):
    collection = get_user_collection(role)
    if collection:
        collection.delete_one({'username': username})
    return redirect(url_for('admin_dashboard', username=session['username']))


@app.route('/teacher_dashboard/<username>')
@login_required(role='teacher')
def teacher_dashboard(username):
    assignments = list(assignments_collection.find({'created_by': username}))
    results = []

    for assignment in assignments:
        submissions = list(answers_collection.find({'assignment_id': str(assignment['_id'])}))
        for sub in submissions:
            student_emotions = list(emotion_collection.find({
                'username': sub['student'],
                'assignment_id': str(assignment['_id'])
            }))
            emotion_counts = Counter(
                [log.get('emotion') for log in student_emotions if log.get('emotion')]
            )
            sub['emotion_counts'] = dict(emotion_counts)
        results.append({'assignment': assignment, 'submissions': submissions})

    return render_template('teacher_dashboard.html', name=username, results=results)


@app.route('/create-assignment', methods=['POST'])
@login_required(role='teacher')
def create_assignment():
    assignment = {
        'title': request.form.get('title', '').strip(),
        'created_by': session['username'],
        'questions': []
    }

    for i in range(1, 11):
        question_text = request.form.get(f'q{i}')
        if question_text:
            question = {
                'question': question_text.strip(),
                'options': {
                    'A': request.form.get(f'q{i}_a'),
                    'B': request.form.get(f'q{i}_b'),
                    'C': request.form.get(f'q{i}_c'),
                    'D': request.form.get(f'q{i}_d')
                },
                'answer': request.form.get(f'q{i}_answer')
            }
            assignment['questions'].append(question)

    assignments_collection.insert_one(assignment)
    return redirect(url_for('teacher_dashboard', username=session['username']))


@app.route('/dashboard/student/<username>')
@login_required(role='student')
def student_dashboard(username):
    assignments = list(assignments_collection.find())
    for a in assignments:
        a['_id'] = str(a['_id'])

    todos = student_todos.get(username, [])

    student = students_collection.find_one({'username': username})
    rollno = student.get('rollno') if student else None

    return render_template(
        'student_dashboard.html',
        name=username,
        rollno=rollno,
        assignments=assignments,
        todos=todos
    )


@app.route('/attempt-assignment/<assignment_id>', methods=['GET', 'POST'])
@login_required(role='student')
def attempt_assignment(assignment_id):
    assignment = assignments_collection.find_one({'_id': ObjectId(assignment_id)})
    if not assignment:
        abort(404, description="Assignment not found.")

    if request.method == 'POST':
        answers = {}
        score = 0

        for i, question in enumerate(assignment['questions']):
            ans = request.form.get(f'answer_{i}')
            correct = question.get('answer')
            answers[str(i)] = ans
            if ans and correct and ans.strip().upper() == correct.strip().upper():
                score += 1

        answers_collection.insert_one({
            'student': session['username'],
            'assignment_id': assignment_id,
            'answers': answers,
            'score': score
        })

        username = session['username']
        if username in active_model_processes:
            pid = active_model_processes[username]
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                print("Error stopping OpenCV process:", e)

            del active_model_processes[username]

        return redirect(url_for('student_results', username=session['username']))

    assignment['_id'] = str(assignment['_id'])
    return render_template('attempt_assignment.html', assignment=assignment)


@app.route('/student-results/<username>')
@login_required(role='student')
def student_results(username):
    submissions = list(answers_collection.find({'student': username}))
    for s in submissions:
        assignment = assignments_collection.find_one({'_id': ObjectId(s['assignment_id'])})
        s['assignment'] = assignment['title'] if assignment else 'Assignment Deleted'
    return render_template('student_results.html', submissions=submissions)


@app.route('/add-todo', methods=['POST'])
@login_required(role='student')
def add_todo():
    username = request.form.get('username')
    task = request.form.get('task', '').strip()
    if task:
        student_todos.setdefault(username, []).append(task)
    return redirect(url_for('student_dashboard', username=username))


@app.route('/delete-todo/<username>/<int:index>', methods=['POST'])
@login_required(role='student')
def delete_todo(username, index):
    if username in student_todos and 0 <= index < len(student_todos[username]):
        del student_todos[username][index]
    return redirect(url_for('student_dashboard', username=username))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/start_model', methods=['POST'])
@login_required()
def start_model():
    username = session.get('username')
    data = request.get_json(silent=True)
    assignment_id = data.get('assignment_id') if data else request.form.get('assignment_id')

    if not username or not assignment_id:
        return jsonify({"status": "error", "message": "Missing data."}), 400

    model_script_path = os.path.abspath("../backend/realtimedetection.py")

    try:
        process = subprocess.Popen([
            sys.executable,
            model_script_path,
            username,
            assignment_id,
            '300'
        ])

        active_model_processes[username] = process.pid
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/emotion-logs')
@login_required(role='admin')
def emotion_logs():
    logs = list(emotion_collection.find())
    return render_template('emotion_logs.html', logs=logs)


@app.route('/get-emotions/<username>/<assignment_id>')
@login_required(role='student')
def get_emotions(username, assignment_id):
    if session.get('username') != username:
        abort(403)

    logs = list(emotion_collection.find({
        'username': username,
        'assignment_id': assignment_id
    }))
    emotion_counts = Counter([log.get('emotion') for log in logs if log.get('emotion')])
    return jsonify(emotion_counts)


@app.route('/performance')
def performance():
    username = request.args.get('username') or session.get('username')
    assignment_id = request.args.get('assignment_id')

    if not username and not assignment_id:
        return "Missing both username and assignment ID", 400
    if not username:
        return "Missing username", 400
    if not assignment_id:
        return "Missing assignment ID", 400

    emotion_logs = emotion_collection.find({
        'username': username,
        'assignment_id': assignment_id
    })
    emotion_counts = {}
    for log in emotion_logs:
        emotion = log['emotion']
        emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1

    return render_template(
        'performance.html',
        username=username,
        assignment_id=assignment_id,
        emotion_counts=emotion_counts
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)