from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern



# Aadhaar (12 digits, grouped or plain)
aadhaar_pattern = Pattern("Aadhaar", r"\b\d{4}\s?\d{4}\s?\d{4}\b", 0.8)
aadhaar_recognizer = PatternRecognizer(supported_entity="AADHAAR_NUMBER",  patterns=[aadhaar_pattern])

# PAN (ABCDE1234F)
pan_pattern = Pattern("PAN", r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", 0.85)
pan_recognizer = PatternRecognizer(supported_entity="PAN_CARD",  patterns=[pan_pattern])

# Voter ID (e.g., ABC1234567)
voter_pattern = Pattern("VoterID", r"\b[A-Z]{3}[0-9]{7}\b", 0.75)
voter_recognizer = PatternRecognizer(supported_entity="VOTER_ID",  patterns=[voter_pattern])

# Driving License (e.g., DL-0420110149646 or TN-09-19860012345)
dl_pattern = Pattern("DL", r"\b[A-Z]{2}[- ]?\d{2}[- ]?\d{4,12}\b", 0.75)
dl_recognizer = PatternRecognizer(supported_entity="INDIAN_DRIVERS_LICENSE",  patterns=[dl_pattern])

# Passport (e.g., N1234567)
passport_pattern = Pattern("Passport", r"\b[A-Z][0-9]{7}\b", 0.8)
passport_recognizer = PatternRecognizer(supported_entity="INDIAN_PASSPORT",  patterns=[passport_pattern])

# Bank Account (9–18 digits)
bank_pattern = Pattern("BankAccount", r"\b\d{9,18}\b", 0.7)
bank_recognizer = PatternRecognizer(supported_entity="INDIAN_BANK_ACCOUNT",  patterns=[bank_pattern])

# IFSC Code (e.g., SBIN0005943)
ifsc_pattern = Pattern("IFSC", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.85)
ifsc_recognizer = PatternRecognizer(supported_entity="IFSC_CODE",  patterns=[ifsc_pattern])

# UPI ID (e.g., riya@oksbi)
upi_pattern = Pattern("UPI", r"\b[\w\.\-]{2,256}@[a-z]{2,64}\b", 0.8)
upi_recognizer = PatternRecognizer(supported_entity="UPI_ID", patterns= [upi_pattern])


analyzer = AnalyzerEngine()

# Add all Indian PII recognizers
for r in [
    aadhaar_recognizer,
    pan_recognizer,
    voter_recognizer,
    dl_recognizer,
    passport_recognizer,
    bank_recognizer,
    ifsc_recognizer,
    upi_recognizer,
]:
    analyzer.registry.add_recognizer(r)