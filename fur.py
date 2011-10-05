from flask import Flask, request, g, session, flash, redirect, url_for, render_template, abort, send_file
from functools import wraps
import os
import pymongo
from werkzeug import secure_filename
import re
import datetime
import tempfile
import shutil
import json
import rpm

app = Flask(__name__)
app.secret_key = "my secret key"
app.config["UPLOAD_FOLDER"] = "/home/tom/vboxshare/fur/packages"
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def login_required_post(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if g.user is None and request.method == "POST":
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def get_package_info(path_to_spec):
    spec = rpm.spec(path_to_spec)
    hdr = spec.sourceHeader
    # need to process spec for requires
    # http://lists.rpm.org/pipermail/rpm-list/2011-September/000992.html
    ret = {}
    for field in ["name", "version", "release", "license", "summary", "url", "changelogtext"]:
        ret[field] = hdr[field]

    return ret
    

@app.before_request
def before_request():
    g.mongo = pymongo.Connection("localhost", 27017)
    g.mongo_db = g.mongo['fur']
    g.users = g.mongo_db['users']
    g.packages = g.mongo_db['packages']
    g.user = None
    if 'username' in session:
        g.user = g.users.find_one({'username': session['username']})

@app.teardown_request
def teardown_request(exception):
    g.mongo.disconnect()

@app.route("/")
def index():
    package_count=g.packages.count()
    recent_packages = g.packages.find().sort("name")[:10]
    return render_template("index.html", package_count=package_count, recent_packages=recent_packages)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")
    else:
        username = request.form["username"]
        email1, email2 = request.form["email1"], request.form["email2"]
        password1, password2 = request.form["password1"], request.form["password2"]
        if email1 != email2:
            return "email addresses do not match"
        if password1 != password2:
            return "passwords do not match"
        if len(password1) < 6 or 30 < len(password1):
            return "error, password must be between 6 and 30 characters long"
        if g.users.find({'username': username}).count():
            return "error creating username %s because it already exists" % username
        g.users.insert({
            'username': username,
            'password': password1,
            'email1': email1
        })
        return "create the user here for %s with password %s" % (username, password1)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    else:
        username, password = request.form["username"], request.form["password"]
        user = g.users.find_one({"username": username})
        if user and user["password"] == password:
            session["username"] = username
            return redirect(url_for("index"))
        else:
            return "invalid username or password"

@app.route("/logout")
def logout():
    session.pop("username", None)
    return redirect(url_for("index"))

@app.route("/packages/<package_name>/", methods=["GET", "POST"])
@login_required_post
def packages(package_name):
    # check if the package exists
    package = g.packages.find_one({"name": package_name})
    if not package:
        abort(404)
    if request.method == "POST":
        # process the data on the page
        if "do_adopt" in request.form:
            if g.user and package["maintainer"] == "orphan":
                package["maintainer"] = g.user["username"]
                g.packages.save(package)
        if "do_disown" in request.form:
            if g.user and package["maintainer"] == g.user["username"]:
                package["maintainer"] = "orphan"
                g.packages.save(package)
        if "do_comment" in request.form:
            if request.form["new_comment"]:
                package["comments"].insert(0, {"date": datetime.datetime.today(),
                                               "submitter": g.user["username"],
                                               "comment": request.form["new_comment"]})
                g.packages.save(package)
        if "do_toggle_outdated" in request.form:
            package["outdated"] = not package["outdated"]
            package["outdated_since"] = datetime.datetime.today()
            g.packages.save(package)
    return render_template("packages.html", package=package)

@app.route("/api/packages/<package_name>/")
def api_packages(package_name):
    package = g.packages.find_one({"name": package_name})
    if not package:
        abort(404)
    del package["_id"]
    package["updated"] = str(package["updated"])
    package["submitted"] = str(package["submitted"])
    package["outdated_since"] = str(package["outdated_since"])
    for comment in package["comments"]:
        comment["date"] = str(comment["date"])
    return json.dumps(package)

@app.route("/packages/<package_name>/<filename>")
def download_file(package_name, filename):
    path = os.path.join(app.config["UPLOAD_FOLDER"], package_name, filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)

# TODO: remove this later!
@app.route("/add/<package>")
def add(package):
    g.packages.insert({"name": package,
                       "version": "1.0",
                       "release": 1,
                       "url": "http://www.google.com",
                       "description": "a new package",
                       "submitter": "tom",
                       "maintainer": "tom",
                       "license": "gpl",
                       "submitted": datetime.datetime.today(),
                       "updated": datetime.datetime.today(),
                       "sources": ["package.sh"],
                       "dependencies": ["gcc", "linux"],
                       "outdated": False,
                       "outdated_since": datetime.datetime.today(),
                       "comments": []
                       })
    return "successfully added"

# TODO: remove this later!
@app.route("/drop")
def drop():
    g.packages.drop()
    return "dropped"

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_package():
    if request.method == "GET":
        return render_template("upload.html")
    else:
        file = request.files["file"]
        if file.filename == "":
            flash("You need to upload a file.")
            return render_template("upload.html")
        filename = secure_filename(file.filename)

        # check if the extension is allowed
        if not filename.endswith(".spec"):
            flash("You must upload file with the ending \".spec\".")
            return render_template("upload.html")

        # save .src.rpm to a temporary directory for processing
        temp_file = os.path.join(tempfile.gettempdir(), filename)
        file.save(temp_file)

        try:
            pkginfo = get_package_info(temp_file)
        except ValueError, err:
            flash("error reading spec file: %s" % err)
            return render_template("upload.html")

        existing_pkg = g.packages.find_one({"name": pkginfo["name"]})
        if existing_pkg:
            # make sure the maintainer is the same as the uploader
            if g.user["username"] != existing_pkg["maintainer"]:
                flash("You must be the maintainer of %s in order to upload it." % pkginfo["name"])
                return render_template("upload.html")

        # make sure the folder exists
        path = os.path.join(app.config["UPLOAD_FOLDER"], pkginfo["name"])
        if not os.path.exists(path):
            os.makedirs(path)

        # updated entry:
        pkg_entry= {"name": pkginfo["name"],
                    "filename": filename,
                    "version": pkginfo["version"],
                    "release": pkginfo["release"],
                    "url": pkginfo["url"],
                    "summary": pkginfo["summary"],
                    "submitter": g.user["username"],
                    "maintainer": g.user["username"],
                    "license": pkginfo["license"],
                    "updated": datetime.datetime.today(),
                    "outdated": False,
                    "outdated_since": datetime.datetime.today(),
                    "comments": [],
                    "changelogtext": pkginfo["changelogtext"][0]}
        if existing_pkg:
            old_rpm_path = os.path.join(path, filename)
            os.remove(old_rpm_path)
            existing_pkg.update(pkg_entry)
            g.packages.save(existing_pkg)
        else:
            # create a new package entry
            pkg_entry["submitted"] = datetime.datetime.today()
            g.packages.insert(pkg_entry)

        # everything checks out, save the file and add it to the db
        shutil.move(temp_file, path)

        return redirect(url_for("packages", package_name=pkginfo["name"]))

@app.route("/search", methods=["POST"])
def search():
    regex = re.compile(r".*%s.*" % request.form["search_box"], re.IGNORECASE)
    search_results = g.packages.find({ "name": regex})
    return render_template("search.html", packages=search_results)

if __name__ == "__main__":
    app.debug = True
    app.run(host="0.0.0.0", port=8080)