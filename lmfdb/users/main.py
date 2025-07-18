# this holds all the flask-login specific logic (+ url mapping an rendering templates)
# for the user management
# author: harald schilly <harald.schilly@univie.ac.at>

import flask
from functools import wraps
from lmfdb.app import app
from lmfdb.logger import make_logger
from flask import render_template, request, Blueprint, url_for, make_response
from flask_login import login_required, login_user, current_user, logout_user, LoginManager
from lmfdb.utils import flash_error, to_dict
from lmfdb.utils.uploader import Uploader
from markupsafe import Markup

from lmfdb import db
assert db


login_page = Blueprint("users", __name__, template_folder='templates')
logger = make_logger(login_page)

import re
allowed_usernames = re.compile("^[a-zA-Z0-9._-]+$")

login_manager = LoginManager()

from .pwdmanager import userdb, LmfdbUser, LmfdbAnonymousUser

base_url = "http://beta.lmfdb.org"

@login_manager.user_loader
def load_user(userid):
    return LmfdbUser(userid)

login_manager.login_view = "users.info"

# this anonymous user has the is_admin() method
login_manager.anonymous_user = LmfdbAnonymousUser


def get_username(uid):
    """returns the name of user @uid"""
    return LmfdbUser(uid).name

# globally define user properties and username


@app.context_processor
def ctx_proc_userdata():
    userdata = {}
    userdata['user_can_write'] = userdb.can_read_write_userdb()
    if not userdata['user_can_write']:
        userdata['userid'] = 'anon'
        userdata['username'] = 'Anonymous'
        userdata['user_is_admin'] = False
        userdata['user_is_authenticated'] = False
        userdata['user_can_review_knowls'] = False
        userdata['get_username'] = LmfdbAnonymousUser().name # this is a function

    else:
        userdata['userid'] = 'anon' if current_user.is_anonymous() else current_user._uid
        userdata['username'] = 'Anonymous' if current_user.is_anonymous() else current_user.name

        userdata['user_is_authenticated'] = current_user.is_authenticated

        userdata['user_is_admin'] = current_user.is_admin()
        userdata['user_can_review_knowls'] = current_user.is_knowl_reviewer()
        userdata['get_username'] = get_username  # this is a function
    return userdata

# blueprint specific definition of the body_class variable


@login_page.context_processor
def body_class():
    return {'body_class': 'login'}

# the following doesn't work as it should, also depends on blinker python lib
# flask signal when a user logs in. we record the last logins in the user's data
# http://flask.pocoo.org/docs/signals/
# def log_login_callback(cur_app, user = None):
#  cur_user = user or current_user
#  logger.info(">> curr_app: %s   user: %s" % (cur_app, cur_user))
#
# from flask.ext.login import user_logged_in, user_login_confirmed
# user_logged_in.connect(log_login_callback)
# user_login_confirmed.connect(log_login_callback)


def base_bread():
    return [('Users', url_for(".list"))]


@login_page.route("/")
def list():
    COLS = 5
    users = userdb.get_user_list()
    # attempt to sort by last name
    users = sorted(users, key=lambda x: x[1].strip().split(" ")[-1].lower())
    if len(users) % COLS:
        users += [{} for i in range(COLS-len(users) % COLS)]
    n = len(users)//COLS
    user_rows = tuple(zip(*[users[i*n: (i + 1)*n] for i in range(COLS)]))
    bread = base_bread()
    return render_template("user-list.html", title="All Users",
                           user_rows=user_rows, bread=bread)


@login_page.route("/change_colors/<int:scheme>")
@login_required
def change_colors(scheme):
    userid = current_user.get_id()
    userdb.change_colors(userid, scheme)
    flask.flash(Markup("Color scheme successfully changed"))
    response = make_response(flask.redirect(url_for(".info")))
    response.set_cookie('color', str(scheme))
    return response

@login_page.route("/myself")
def info():
    info = {}
    info['login'] = url_for(".login")
    info['logout'] = url_for(".logout")
    info['user'] = current_user
    info['next'] = request.referrer
    from lmfdb.utils.color import all_color_schemes
    return render_template("user-info.html",
                           all_colors=all_color_schemes.values(),
                           info=info, title="Userinfo",
                           bread=base_bread() + [("Myself", url_for(".info"))])

# ./info again, but for POST!


@login_page.route("/info", methods=['POST'])
@login_required
def set_info():
    for k, v in request.form.items():
        if v:
            setattr(current_user, k, v)
    current_user.save()
    flask.flash(Markup("Thank you for updating your details!"))
    return flask.redirect(url_for(".info"))


@login_page.route("/profile/<userid>")
def profile(userid):
    user = LmfdbUser(userid)
    if not user.exists:
        flash_error("User %s does not exist", userid)
        return flask.redirect(url_for(".list"))
    bread = base_bread() + [(user.name, url_for('.profile', userid=user.get_id()))]
    from lmfdb.knowledge.knowl import knowldb
    userknowls = knowldb.search(author=userid, sort=['title'])
    return render_template("user-detail.html", user=user,
                           title="%s" % user.name, bread=bread, userknowls=userknowls)


@login_page.route("/login", methods=["POST"])
def login(**kwargs):
    """
    login and validate the user …
    remember = True sets a cookie to remember the user
    """
    name = request.form["name"]
    password = request.form["password"]
    nxt = request.form["next"]
    remember = request.form.get("remember") == "on"
    user = LmfdbUser(name)
    if user and user.authenticate(password):
        login_user(user, remember=remember)
        flask.flash(Markup("Hello %s, your login was successful!" % user.name))
        logger.info("login: '%s' - '%s'" % (user.get_id(), user.name))
        # FIXME add color cookie, see change_colors
        return flask.redirect(nxt or url_for(".info"))
    flash_error("Oops! Wrong username or password.")
    return flask.redirect(url_for(".info"))


def admin_required(fn):
    """
    wrap this around those entry points where you need to be an admin.
    """
    @wraps(fn)
    @login_required
    def decorated_view(*args, **kwargs):
        logger.info("admin access attempt by %s" % current_user.get_id())
        if not current_user.is_admin():
            return flask.abort(403)  # access denied
        return fn(*args, **kwargs)
    return decorated_view

def knowl_reviewer_required(fn):
    """
    wrap this around those entry points where you need to be a knowl reviewer.
    """
    @wraps(fn)
    @login_required
    def decorated_view(*args, **kwargs):
        logger.info("reviewer access attempt by %s" % current_user.get_id())
        if not current_user.is_knowl_reviewer():
            return flask.abort(403)  # access denied
        return fn(*args, **kwargs)
    return decorated_view

def housekeeping(fn):
    """
    wrap this around maintenance calls, they are only accessible for
    admins and for localhost
    """
    @wraps(fn)
    def decorated_view(*args, **kwargs):
        logger.info("housekeeping access attempt by %s" % request.remote_addr)
        if request.remote_addr in ["127.0.0.1", "localhost"]:
            return fn(*args, **kwargs)
        return admin_required(fn)(*args, **kwargs)
    return decorated_view


@login_page.route("/register")
def register_new():
    return ""

@login_page.route("/register/new")
@login_page.route("/register/new/<int:N>")
@admin_required
def register(N=10):
    N = min(N, 100)
    import random
    tokens = [str(random.randrange(1e20, 1e21)) for _ in range(N)]
    userdb.create_tokens(tokens)
    urls = ["%s%s" % (base_url, url_for(".register_token", token=t)) for t in tokens]
    resp = make_response('\n'.join(urls))
    resp.headers['Content-type'] = 'text/plain'
    return resp


@login_page.route("/register/<token>", methods=['GET', 'POST'])
def register_token(token):
    if not userdb._rw_userdb:
        flask.abort(401, "no attempt to create user, not enough privileges")
    userdb.delete_old_tokens()
    if not userdb.token_exists(token):
        flask.abort(401)
    bread = base_bread() + [('Register', url_for(".register_new"))]
    if request.method != 'POST':
        return render_template("register.html", title="Register", bread=bread, next=request.referrer or "/", token=token)
    else: # must be post
        name = request.form['name']
        if not allowed_usernames.match(name):
            flash_error("""Oops, usename '%s' is not allowed.
                  It must consist of lower/uppercase characters,
                  no spaces, numbers or '.', '_' and '-'.""", name)
            return flask.redirect(url_for(".register_new"))

        pw1 = request.form['password1']
        pw2 = request.form['password2']
        if pw1 != pw2:
            flash_error("Oops, passwords do not match!")
            return flask.redirect(url_for(".register_new"))

        if len(pw1) < 8:
            flash_error("Oops, password too short. Minimum 8 characters please!")
            return flask.redirect(url_for(".register_new"))

        full_name = request.form['full_name']
        #next = request.form["next"]

        if userdb.user_exists(name):
            flash_error("Sorry, user ID '%s' already exists!", name)
            return flask.redirect(url_for(".register_new"))

        newuser = userdb.new_user(name, pwd=pw1, full_name=full_name)
        userdb.delete_token(token)
        #newuser.full_name = full_name
        #newuser.save()
        login_user(newuser, remember=True)
        flask.flash(Markup("Hello %s! Congratulations, you are a new user!" % newuser.name))
        logger.debug("removed login token '%s'" % token)
        logger.info("new user: '%s' - '%s'" % (newuser.get_id(), newuser.name))
        return flask.redirect(url_for(".info"))


@login_page.route("/change_password", methods=['POST'])
@login_required
def change_password():
    uid = current_user.get_id()
    pw_old = request.form['oldpwd']
    if not current_user.authenticate(pw_old):
        flash_error("Ooops, old password is wrong!")
        return flask.redirect(url_for(".info"))

    pw1 = request.form['password1']
    pw2 = request.form['password2']
    if pw1 != pw2:
        flash_error("Oops, new passwords do not match!")
        return flask.redirect(url_for(".info"))

    userdb.change_password(uid, pw1)
    flask.flash(Markup("Your password has been changed."))
    return flask.redirect(url_for(".info"))


@login_page.route("/logout")
@login_required
def logout():
    # FIXME delete color cookie
    logout_user()
    flask.flash(Markup("You are logged out now. Have a nice day!"))
    return flask.redirect(request.args.get("next") or request.referrer or url_for('.info'))


@login_page.route("/admin")
@admin_required
def admin():
    return "success: only admins can read this!"


@app.route("/restartserver")
@admin_required
def restart():
    from subprocess import Popen, PIPE
    from urllib.parse import urlparse
    urlparts = urlparse(request.url)
    if urlparts.netloc == "beta.lmfdb.org":
        command = ['bash', '/home/lmfdb/restart-dev']
    elif urlparts.netloc in ["prodweb1.lmfdb.xyz", "prodweb2.lmfdb.xyz"]:
        command = ['bash', '/home/lmfdb/restart-web']
    else:
        command = None
    if command:
        out = Popen(command, stdout=PIPE, encoding='utf-8').communicate()[0]
        return out.replace('\n', '<br>')
    else:
        return "Only supported in beta.lmfdb.org, prodweb1.lmfdb.xyz, and prodweb2.lmfdb.xyz"


class Reviewer(Uploader):
    """
    This uploader is used to collect UploadSection objects from different sections of the LMFDB that use them.
    """
    def __init__(self):
        from lmfdb.modular_curves.upload import Points, PointCompleteness, GonalityBounds, Models, UniversalEC, MultiKnowl
        super().__init__([Points(), PointCompleteness(), GonalityBounds(), Models(), UniversalEC(), MultiKnowl()])


@login_page.route("/uploads", methods=["GET", "POST"])
@login_required
def review_uploads():
    uploader = Reviewer()
    reviewer = current_user.is_knowl_reviewer()
    if request.method == "POST":
        info = to_dict(request.form)
        uploader.review(info, reviewer, current_user.id)
    else:
        info = to_dict(request.args)

    user_shown = info.get("user_shown", "" if reviewer else current_user.id)
    reviewing = (reviewer and not user_shown)
    results, statuses = uploader.show_uploads(info, reviewing, user_shown)

    return render_template(
        "user-uploads.html",
        title="Review data uploads",
        uploader=uploader,
        statuses=statuses,
        results=results,
        user_shown=user_shown,
    )
