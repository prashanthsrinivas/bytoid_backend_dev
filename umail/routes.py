from flask import Flask, request, jsonify, Blueprint, Response, session


umail_bp = Blueprint("umail_webhook", __name__)