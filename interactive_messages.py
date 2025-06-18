# interactive_messages.py

initial_greeting_message = {
    "type": "button",
    "view_once": True,
    "body": {
        "text": {
            "ar": "هلا ! 👋\nأنا مساعدك الآلي في تطبيق وصلت.\n\nتقدر تعرف من خلالي أكثر عن خدمات وصلت وكيف ممكن أساعدك سواء كنت مالك أو مستأجر.",
            "en": "Hello! 👋\nI am your automated assistant in the Wasalt app.\n\nThrough me, you can learn more about Wasalt's services and how I can help you, whether you are an owner or a tenant."
        }
    },
    "footer": {
        "text": {
            "ar": "اختر واحدة من الخيارات التالية:",
            "en": "Choose one of the following options:"
        }
    },
    "action": {
        "buttons": [
            {
                "type": "reply",
                "reply": {
                    "id": "button_id1",
                    "title": {
                        "ar": "أملك شقة وحاب أشغلها",
                        "en": "I own an apartment and want to operate it"
                    }
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": "button_id2",
                    "title": {
                        "ar": "أبي أستأجر شقة",
                        "en": "I want to rent an apartment"
                    }
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": "button_id3",
                    "title": {
                        "ar": "أبي أتواصل مع خدمة العملاء",
                        "en": "I want to contact customer service"
                    }
                }
            }
        ]
    }
}

owner_options_message = {
    "type": "button",
    "view_once": True,
    "body": {
        "text": {
            "ar": "تمام، شقتك مؤثثة ولا لا؟ 🤔",
            "en": "Okay, is your apartment furnished or not? 🤔"
        }
    },
    "action": {
        "buttons": [
            {
                "type": "reply",
                "reply": {
                    "id": "button_id4",
                    "title": {
                        "ar": "نعم مؤثثة",
                        "en": "Yes, furnished"
                    }
                }
            },
            {
                "type": "reply",
                "reply": {
                    "id": "button_id5",
                    "title": {
                        "ar": "لا غير مؤثثة",
                        "en": "No, unfurnished"
                    }
                }
            }
        ]
    }
}

furnished_apartment_message = {
    "type": "button",
    "view_once": True,
    "body": {
        "text": {
            "ar": "حلو، وصلت تقدم لك خدمة إدارة وتأجير الشقق المفروشة بالنيابة عنك.\n\nتقدر تعرف أكثر من خلال موقعنا الإلكتروني وتقدر كمان تسجل شقتك من خلال الرابط التالي:",
            "en": "Great, Wasalt offers you the service of managing and renting furnished apartments on your behalf.\n\nYou can find out more through our website and you can also register your apartment through the following link:"
        }
    },
    "action": {
        "buttons": [
            {
                "type": "url",
                "reply": {
                    "id": "button_id7",
                    "title": {
                        "ar": "سجل شقتك",
                        "en": "Register your apartment"
                    },
                    "url": "https://wasalt.com/owner-register"
                }
            }
        ]
    }
}

unfurnished_apartment_message = {
    "type": "button",
    "view_once": True,
    "body": {
        "text": {
            "ar": "حلو، وصلت تقدم لك خدمة إدارة وتأجير الشقق غير المفروشة بالنيابة عنك.\n\nتقدر تعرف أكثر من خلال موقعنا الإلكتروني وتقدر كمان تسجل شقتك من خلال الرابط التالي:",
            "en": "Great, Wasalt offers you the service of managing and renting unfurnished apartments on your behalf.\n\nYou can find out more through our website and you can also register your apartment through the following link:"
        }
    },
    "action": {
        "buttons": [
            {
                "type": "url",
                "reply": {
                    "id": "button_id8",
                    "title": {
                        "ar": "سجل شقتك",
                        "en": "Register your apartment"
                    },
                    "url": "https://wasalt.com/owner-register"
                }
            }
        ]
    }
}

tenant_options_message = {
    "type": "list",
    "view_once": True,
    "header": {
        "type": "text",
        "text": {
            "ar": "اختيارك موفق! وصلت يوفر لك أفضل الشقق وبأفضل الأسعار.",
            "en": "Good choice! Wasalt provides you with the best apartments at the best prices."
        }
    },
    "body": {
        "text": {
            "ar": "تقدر تبحث عن شقة حسب المدينة اللي تناسبك، تفضل قائمة بالمدن المتوفرة حاليًا:",
            "en": "You can search for an apartment according to the city that suits you. Here is a list of currently available cities:"
        }
    },
    "footer": {
        "text": {
            "ar": "اختر المدينة لعرض الشقق المتاحة",
            "en": "Choose the city to display available apartments"
        }
    },
    "action": {
        "button": {
            "ar": "المدن",
            "en": "Cities"
        },
        "sections": [
            {
                "title": {
                    "ar": "اختر مدينة",
                    "en": "Select a city"
                },
                "rows": [
                    {
                        "id": "row_id1",
                        "title": {
                            "ar": "الرياض",
                            "en": "Riyadh"
                        },
                        "description": {
                            "ar": "العاصمة وأكبر مدن المملكة",
                            "en": "The capital and largest city of the Kingdom"
                        }
                    },
                    {
                        "id": "row_id2",
                        "title": {
                            "ar": "جدة",
                            "en": "Jeddah"
                        },
                        "description": {
                            "ar": "عروس البحر الأحمر",
                            "en": "The Bride of the Red Sea"
                        }
                    },
                    {
                        "id": "row_id3",
                        "title": {
                            "ar": "الدمام",
                            "en": "Dammam"
                        },
                        "description": {
                            "ar": "المركز الإداري للمنطقة الشرقية",
                            "en": "The administrative center of the Eastern Province"
                        }
                    },
                    {
                        "id": "row_id4",
                        "title": {
                            "ar": "مكة المكرمة",
                            "en": "Makkah Al-Mukarramah"
                        },
                        "description": {
                            "ar": "أقدس بقاع الأرض لدى المسلمين",
                            "en": "The holiest place on earth for Muslims"
                        }
                    },
                    {
                        "id": "row_id5",
                        "title": {
                            "ar": "المدينة المنورة",
                            "en": "Al-Madinah Al-Munawwarah"
                        },
                        "description": {
                            "ar": "ثاني أقدس المدن الإسلامية",
                            "en": "The second holiest city in Islam"
                        }
                    }
                ]
            }
        ]
    }
}
