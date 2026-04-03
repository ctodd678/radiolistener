import smtplib
import json
import time
from email.message import EmailMessage

def test_email_blast():
    print("--- Starting Email Configuration Test ---")
    
    # 1. Load Config
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        print("✅ config.json loaded successfully.")
    except Exception as e:
        print(f"❌ Error loading config.json: {e}")
        return

    sender = config['sender_email']
    password = config['app_password']
    recipients = config['recipients']

    # 2. Generate the "Test Hour" Label (matches main script logic)
    hour_label = time.strftime("%I:00%p").lstrip('0')
    
    print(f"Attempting to send from: {sender}")
    print(f"Targeting recipients: {', '.join(recipients)}")
    print(f"Subject will be: 🚨 {hour_label} Keyword Alert")
    print("-" * 30)

    # 3. Execution
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            print("✅ Authentication successful.")

            for recipient in recipients:
                msg = EmailMessage()
                msg.set_content(
                    f"This is a manual test of the Radio Scout system.\n\n"
                    f"If you are seeing this, the {hour_label} logic is working "
                    f"and the email blast is bypasssing spam filters.\n\n"
                    f"System Time: {time.strftime('%H:%M:%S')}"
                )
                msg["Subject"] = f"🚨 {hour_label} Keyword Alert (TEST)"
                msg["From"] = sender
                msg["To"] = recipient

                server.send_message(msg)
                print(f"   ✅ Email sent to: {recipient}")
                time.sleep(0.5)
        
        print("-" * 30)
        print("--- TEST COMPLETE: Check your inboxes! ---")

    except smtplib.SMTPAuthenticationError:
        print("❌ FAILED: Gmail rejected your credentials. Check your App Password.")
    except Exception as e:
        print(f"❌ FAILED: An unexpected error occurred: {e}")

if __name__ == "__main__":
    test_email_blast()