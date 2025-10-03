import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-change-this-in-production-12345'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///users.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # BigQuery Configuration
    GOOGLE_APPLICATION_CREDENTIALS = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    BIGQUERY_PROJECT_ID = os.environ.get('BIGQUERY_PROJECT_ID')
    
    # Retail Express API Configuration
    RETAIL_EXPRESS_API_KEY = os.environ.get('RETAIL_EXPRESS_API_KEY')
    RETAIL_EXPRESS_BASE_URL = os.environ.get('RETAIL_EXPRESS_BASE_URL', 'https://api.retailexpress.com.au')


