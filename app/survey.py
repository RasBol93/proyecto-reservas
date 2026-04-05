# app/survey.py
#
# Fachada de compatibilidad para el sistema de encuestas.
# Mantiene los imports históricos:
#   from app.survey import ...
#
# La lógica real vive ahora en:
# - app.survey_core
# - app.survey_settings
# - app.survey_questions
# - app.survey_runtime
# - app.survey_analytics

from app.survey_core import *
from app.survey_settings import *
from app.survey_questions import *
from app.survey_runtime import *
from app.survey_analytics import *
