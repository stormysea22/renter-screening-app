"""
Run:  pip install -r requirements.txt && flask --app app run --debug
"""
from datetime import datetime
import random, time
import openai, re, os, uuid
from functools import wraps
from flask import Flask, render_template, redirect, url_for, flash, request, g
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.monitor.opentelemetry import configure_azure_monitor
from config.logging import setup_logging, setup_db_logging, log_db_operation, setup_request_logging

# Initialize app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['USE_LOCAL_STORAGE'] = os.getenv('USE_LOCAL_STORAGE', 'false')
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', os.path.join(app.root_path, 'static', 'uploads'))
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_timeout': 30,
    'pool_recycle': 1800,
    'pool_pre_ping': True
}

# Setup enhanced logging
logger = setup_logging(app)

db = SQLAlchemy(app)
with app.app_context():
    setup_db_logging(app, db)
    setup_request_logging(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# -------------------- Models -------------------- #
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'renter' | 'landlord'
    houses = db.relationship('House', backref='landlord', lazy=True)
    applications = db.relationship('Application', backref='renter', lazy=True)
    active = db.Column(db.Boolean(), default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        app.logger.info(
            f"""Password hash updated for: {
                str(
                    {'user_id': self.id}
                )
            }"""
        )

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class House(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    rent = db.Column(db.Integer, nullable=False)
    landlord_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    photo       = db.Column(db.String(200))  
    applications = db.relationship('Application', backref='house', lazy=True)
    active = db.Column(db.Boolean(), default=True)

class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='pending')
    phone = db.Column(db.String(20))
    move_in = db.Column(db.Date)
    notes = db.Column(db.Text)
    credit_score = db.Column(db.Integer)
    skip_trace = db.Column(db.JSON)
    income_summary = db.Column(db.JSON)
    house_id = db.Column(db.Integer, db.ForeignKey('house.id'), nullable=False)
    renter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ai_score = db.Column(db.Integer) #1-10
    ai_assessment = db.Column(db.String(200))   # short rationale
    photo = db.Column(db.String(200))   # holds filename or blob URL
    active = db.Column(db.Boolean(), default=True)



@login_manager.user_loader
@log_db_operation('query')
def load_user(user_id):
    return User.query.get(int(user_id))


@log_db_operation('insert')
def create_user(name, email, role, password):
    user = User(name=name, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user

# -------------------- Helpers -------------------- #
def run_background_checks(app_obj):
    """Synchronous fake‑API calls; replace with real HTTP requests later."""
    # ‑‑ credit score
    app_obj.credit_score = random.randint(550, 800)

    # ‑‑ skip trace
    app_obj.skip_trace = {
        "emails": [f"{app_obj.renter.email}"],
        "phones": [f"555‑{random.randint(100,999)}‑{random.randint(1000,9999)}"],
        "addresses": [
            {"street": "123 Main St", "city": "Springfield", "state": "IL"}
        ]
    }

    # ‑‑ income
    app_obj.income_summary = {
        "employer": "Acme Corp",
        "monthly_income": random.randint(3000, 8000)
    }

    # pretend the vendor took some time
    time.sleep(1)
    
    # ‑‑ AI rating --#
    
openai.api_key = os.getenv('OPENAI_API_KEY')
GPT_MODEL = "gpt-4o-mini"

def fetch_ai_rating(app_obj):
    """
    Returns (score:int, assessment:str).
    Caches both in the DB to avoid repeat costs.
    """
    try:
        if app_obj.ai_score and app_obj.ai_assessment:
            return app_obj.ai_score, app_obj.ai_assessment

        credit = app_obj.credit_score
        income = (app_obj.income_summary or {}).get("monthly_income")

        if credit is None or income is None:
            return None, "Awaiting data"

        prompt = (
            "You are an underwriting assistant for a rental property manager. "
            "Given a prospective tenant's credit score and monthly income, "
            "do two things:\n"
            "1. Assign a single whole‑number rating from 1 (very high risk) to 10 (very low risk).\n"
            "2. Provide a one‑sentence assessment (max 20 words) explaining the rating.\n\n"
            f"Credit score: {credit}\n"
            f"Monthly income (USD): {income}\n\n"
            "Respond in the format: <score>|<assessment>"
        )

        resp = openai.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.2
        )
        text = resp.choices[0].message.content.strip()

        # Parse "8|Good credit and income indicate low risk."
        if "|" in text:
            score_part, assess = map(str.strip, text.split("|", 1))
        else:
            score_part, assess = text.split()[0], " ".join(text.split()[1:])

        match = re.search(r"\d+", score_part)
        score = int(match.group()) if match else 1
        score = max(1, min(score, 10))
        assess = assess[:180]  # safety truncate

        # Cache
        app_obj.ai_score = score
        app_obj.ai_assessment = assess
        db.session.commit()
        app.logger.info(f"AI rating complete for application {app_obj.id}: {score}")
        return score, assess
    except Exception as e:
        app.logger.error(f"AI rating failed for application {app_obj.id}: {str(e)}")
        return 1, "Error generating rating"

app.jinja_env.globals["fetch_ai_rating"] = fetch_ai_rating

# -------------------- File Uploads -------------------- #
UPLOAD_DIR = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_ext(fname):
    return '.' in fname and fname.rsplit('.', 1)[1].lower() in ALLOWED_EXT

# Blob config
BLOB_CONN = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
BLOB_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "house-photos")

blob_service = None
if BLOB_CONN:
    blob_service = BlobServiceClient.from_connection_string(BLOB_CONN)
    # ensure container exists
    try:
        blob_service.create_container(BLOB_CONTAINER, public_access="blob")
    except Exception:
        pass  # already exists

def upload_to_blob(file_stream, filename, mimetype):
    """
    Uploads a file‑like object to Azure Blob and returns the public URL.
    Assumes container ACL is 'Blob' (public read).
    """
    if not blob_service:
        return None  # config missing

    blob = blob_service.get_blob_client(container=BLOB_CONTAINER, blob=filename)
    blob.upload_blob(
        file_stream,
        overwrite=True,
        content_settings=ContentSettings(content_type=mimetype)
    )
    return blob.url


# -------------------- Routes -------------------- #
@app.route('/')
def home():
    houses = House.query.filter_by(active=True).all()
    return render_template('home.html', houses=houses)

# Update the signup route
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        app.logger.info(f"New signup attempt for email: {request.form['email']}")
        try:
            role = request.form['role']
            user = User(name=request.form['name'], email=request.form['email'], role=role)
            user.set_password(request.form['password'])
            db.session.add(user)
            db.session.commit()
            app.logger.info(f"New user created: {user.email} with role: {user.role}")
            flash('Account created! Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            app.logger.error(f"Signup failed for {request.form['email']}: {str(e)}")
            db.session.rollback()
            flash('Error creating account', 'danger')
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and user.check_password(request.form['password']) and user.active == True:
            app.logger.info(f"User {user.email} logged in")
            login_user(user)
            return redirect(url_for('home'))
        flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

# ----------- Landlord: create house ----------- #
@app.route('/houses/new', methods=['GET', 'POST'])
@login_required
def new_house():
    if current_user.role != 'landlord':
        app.logger.warning(f"Non-landlord user {current_user.email} attempted to create house listing")
        flash('Only landlords can add houses.', 'warning')
        return redirect(url_for('home'))

    if request.method == 'POST':
        app.logger.info(f"New house listing attempt by {current_user.email}")
        try:
            title = request.form['title']
            description = request.form['description']
            rent = request.form['rent']
            file = request.files.get('photo')
            photo_url = None
            # if file and allowed_ext(file.filename):
            #     ext = file.filename.rsplit('.', 1)[1].lower()
            #     filename = f"{uuid.uuid4().hex}.{ext}"
            #     # --- upload to Azure Blob instead of local ---
            #     photo_url = upload_to_blob(file, filename, file.mimetype)
            # elif file:
            #     flash('Invalid image format.', 'warning')
            if file and allowed_ext(file.filename):
            # For local storage:
                if app.config["USE_LOCAL_STORAGE"] == "true":  # This could be a flag or configuration setting
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                    file.save(file_path)
                    photo_url = filename  # Or store a relative path to file

                # For Azure Blob Storage:
                else:
                    ext = file.filename.rsplit('.', 1)[1].lower()
                    filename = f"{uuid.uuid4().hex}.{ext}"
                    photo_url = upload_to_blob(file, filename, file.mimetype)
            else:
                photo_url = None


            house = House(
                title=title,
                description=description,
                rent=int(rent),
                landlord_id=current_user.id,
                photo=photo_url
            )
            db.session.add(house)
            db.session.commit()
            app.logger.info(f"New house listed: {house.id} by {current_user.email}")
            flash('House listed!', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            app.logger.error(f"House listing failed: {str(e)}")
            db.session.rollback()
            flash('Error creating listing', 'danger')

    return render_template('new_house.html')


# (optional) Single‑house detail page for anyone to view
@app.route('/house/<int:house_id>')
def house_detail(house_id):
    house = House.query.get_or_404(house_id)
    return render_template('house_detail.html', house=house)

# ----------- Landlord: delete house ----------- #

@app.route("/houses/<int:house_id>/delete", methods=["POST"])
@login_required
def delete_house(house_id):
    house = House.query.get_or_404(house_id)

    # Only the owner can delete
    if house.landlord_id != current_user.id:
        flash("Not authorized.", "warning")
        return redirect(url_for("dashboard"))

    # Soft delete - set active flag to False
    house.active = False
    # Also deactivate all associated applications
    for application in house.applications:
        application.active = False
    
    db.session.commit()
    app.logger.info(f"House {house_id} and its applications soft-deleted by user {current_user.id}")

    flash("House removed from listings.", "info")
    return redirect(url_for("dashboard"))

# ----------- Renter: application form ----------- #
@app.route('/apply/<int:house_id>', methods=['GET', 'POST'])
@login_required
def apply(house_id):
    if current_user.role != 'renter':
        app.logger.warning(f"Non-renter user {current_user.email} attempted to submit application")
        flash('Only renters can apply.', 'warning')
        return redirect(url_for('home'))

    house = House.query.get_or_404(house_id)
    
    if request.method == 'POST':
        app.logger.info(f"New application attempt for house {house_id} by {current_user.email}")
        try:
            phone = request.form['phone']
            move_in = request.form['move_in']
            notes = request.form['notes']

            app_obj = Application(
                house_id=house_id,
                renter_id=current_user.id,
                phone=phone,
                move_in=datetime.strptime(move_in, "%Y-%m-%d").date(),
                notes=notes
            )
            db.session.add(app_obj)
            db.session.commit()

            app.logger.info(f"Running background checks for application {app_obj.id}")
            run_background_checks(app_obj)
            db.session.commit()
            
            app.logger.info(f"Fetching AI rating for application {app_obj.id}")
            fetch_ai_rating(app_obj)
            
            app.logger.info(f"Application {app_obj.id} submitted successfully")
            flash('Application submitted!', 'success')
            return redirect(url_for('home'))
        except Exception as e:
            app.logger.error(f"Application submission failed: {str(e)}")
            db.session.rollback()
            flash('Error submitting application', 'danger')

    # GET – show the form
    return render_template('application_form.html', house=house)


# ----------- Landlord dashboard ----------- #
@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role != 'landlord':
        flash('Access denied', 'warning')
        return redirect(url_for('home'))
    houses = House.query.filter_by(landlord_id=current_user.id, active=True).all()
    return render_template('dashboard.html', houses=houses)

@app.route('/applications/<int:house_id>')
@login_required
def view_applications(house_id):
    house = House.query.get_or_404(house_id)
    if house.landlord_id != current_user.id:
        flash('Access denied', 'warning')
        return redirect(url_for('home'))
    applications = Application.query.filter_by(house_id=house_id, active=True).all()
    return render_template('applications.html', house=house, applications=applications)

@app.route('/applications/<int:app_id>/set/<string:new_status>', methods=['POST'])
@login_required
def set_status(app_id, new_status):
    app_obj = Application.query.get_or_404(app_id)
    house = app_obj.house
    if house.landlord_id != current_user.id or new_status not in ['approved', 'denied']:
        flash('Action not allowed', 'warning')
        return redirect(url_for('home'))
    app_obj.status = new_status
    db.session.commit()
    flash(f'Application {new_status}.', 'info')
    return redirect(url_for('view_applications', house_id=house.id))
