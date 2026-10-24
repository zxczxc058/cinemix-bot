"""Fixed diagnostics only: never include provider exceptions or session URLs."""
class DriveUploadError(ValueError):
    MESSAGES = {
        "google_reconnect": "חיבור Google חסר או פג תוקף. לחץ על התחברות/החלפת חשבון Google ואשר מחדש גישה ל-Drive.",
        "google_quota": "האחסון בחשבון Google מלא. פנה מקום או התחבר לחשבון אחר.",
        "google_permission": "Google דחה את הרשאת ההעלאה. לחץ על התחברות/החלפת חשבון Google ואשר מחדש הרשאת יצירה והעלאת קבצים ב-Drive. רענון אסימון אינו מוסיף הרשאה שלא אושרה.",
        "google_unavailable": "Google Drive אינו זמין כרגע. ההעלאה נעצרה בבטחה; ניתן לנסות שוב.",
        "telegram_media": "הקובץ אינו נגיש להורדה מטלגרם. שלח או העבר מחדש את הקובץ לבוט ובדוק הרשאות גישה.",
    }

    def __init__(self, code):
        self.code = code if code in self.MESSAGES else "google_unavailable"
        super().__init__(self.code)

    def user_message(self):
        return self.MESSAGES[self.code]
