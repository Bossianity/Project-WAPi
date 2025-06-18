# interactive_messages.py
# Stores components for interactive messages with translations.
# script.py will use these components to build the final JSON payloads
# for whatsapp_utils.py functions.

initial_greeting_message_components = {
    "header": {"ar": "هلا ! أنا مساعد من شركة عوجا لإدارة الأملاك", "en": "Hello! I am an assistant from Awja Property Management"},
    "body": {"ar": "كيف ممكن أخدمك اليوم؟", "en": "How can I help you today?"},
    "footer": {"ar": "أضغط لتختار:", "en": "Click to choose:"},
    "buttons": [
        {"id": "button_id1", "type": "quick_reply", "title": {"ar": "أملك شقة وحاب أشغلها", "en": "I own an apartment I want to operate"}},
        {"id": "button_id2", "type": "quick_reply", "title": {"ar": "ابي أستاجر شقة", "en": "I want to rent an apartment"}},
        {"id": "button_id3", "type": "quick_reply", "title": {"ar": "أستفسارات أخرى", "en": "Other inquiries"}}
    ]
}

owner_options_message_components = {
    "header": {"ar": "نتشرف بيك!", "en": "We are honored to have you!"},
    "body": {"ar": "بس حابين نعرف اذا هي مؤثثة(مفروشة) أو لا؟", "en": "We just want to know if it is furnished or not?"},
    "footer": {"ar": "أضغط لتختار:", "en": "Click to choose:"},
    "buttons": [
        {"id": "button_id4", "type": "quick_reply", "title": {"ar": "نعم مؤثثة", "en": "Yes, furnished"}},
        {"id": "button_id5", "type": "quick_reply", "title": {"ar": "لا غير مؤثثة", "en": "No, unfurnished"}}
    ]
}

furnished_apartment_message_components = {
    "header": {"ar": "الرجاء ملء الاستبيان", "en": "Please fill out the survey"},
    "body": {"ar": "عشان نقدر نخدمك ممكن تملى الإستبيان؟", "en": "So we can serve you, can you fill out the survey?"},
    "footer": {"ar": "أضغط لفتح الرابط:", "en": "Click to open the link:"},
    "buttons": [
        {"id": "button_id7", "type": "url", "title": {"ar": "استبيان الشقق المؤثثة", "en": "Furnished Apartments Survey"}, "url": "https://form.typeform.com/to/eFGv4yhC"}
    ]
}

unfurnished_apartment_message_components = {
    "header": {"ar": "ولا يهمك، عندنا خدمة تأثيث بمعايير فندقية وأسعار تنافسية", "en": "No worries, we have a furnishing service with hotel standards and competitive prices"},
    "body": {"ar": "مهندسينا خبرتهم أكثر من 8 سنوات ومنفذين فوق 500 مشروع.", "en": "Our engineers have more than 8 years of experience and have completed over 500 projects."},
    "footer": {"ar": "فقط عبي الإستبيان:", "en": "Just fill out the survey:"},
    "buttons": [
        {"id": "button_id8", "type": "url", "title": {"ar": "استبيان التأثيث", "en": "Furnishing Survey"}, "url": "https://form.typeform.com/to/vDKXMSaQ"}
    ]
}

tenant_options_message_components = {
    "header": {"ar": "إختيار المدينة", "en": "Select City"},
    "body": {"ar": "في أي مدينة تبغي تحجز؟", "en": "In which city do you want to book?"},
    "footer": {"ar": "إختار من القائمة:", "en": "Choose from the list:"},
    "list_action": { # This key indicates it's a list message
        "label": {"ar": "قائمة المدن السعودية", "en": "List of Saudi Cities"},
        "sections": [
            {
                "title": {"ar": "قائمة المدن السعودية", "en": "List of Saudi Cities"},
                "rows": [
                    {"id": "row_id1", "title": {"ar": "الرياض", "en": "Riyadh"}},
                    {"id": "row_id2", "title": {"ar": "جدة", "en": "Jeddah"}},
                    {"id": "row_id3", "title": {"ar": "مكة المكرمة", "en": "Makkah Al-Mukarramah"}},
                    {"id": "row_id4", "title": {"ar": "المدينة المنورة", "en": "Al-Madinah Al-Munawwarah"}},
                    {"id": "row_id5", "title": {"ar": "الدمام", "en": "Dammam"}},
                    {"id": "row_id6", "title": {"ar": "تبوك", "en": "Tabuk"}},
                    {"id": "row_id7", "title": {"ar": "بريدة", "en": "Buraidah"}},
                    {"id": "row_id8", "title": {"ar": "الطائف", "en": "Taif"}},
                    {"id": "row_id9", "title": {"ar": "خميس مشيط", "en": "Khamis Mushait"}},
                    {"id": "row_id10", "title": {"ar": "حائل", "en": "Hail"}}
                ]
            }
        ]
    }
}

# Placeholder for a potential "other inquiries" message if it becomes interactive
# other_inquiries_message_components = {
# "header": {"ar": "استفسارات أخرى", "en": "Other Inquiries"},
# "body": {"ar": "الرجاء توضيح استفسارك ليتم تحويله للفريق المختص.", "en": "Please clarify your inquiry to be forwarded to the concerned team."},
# "footer": {"ar": "فريقنا في خدمتك", "en": "Our team is at your service"},
# "buttons": [
# {"id": "button_id_manual_input", "type": "quick_reply", "title": {"ar": "كتابة الاستفسار", "en": "Write my inquiry"}}
# ]
# }
