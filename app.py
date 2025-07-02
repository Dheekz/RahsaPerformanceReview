# app.py
# Jalankan dengan: streamlit run app.py

import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore_v1.base_query import FieldFilter
import pandas as pd
import time
import google.generativeai as genai
from config import API_KEY

# --- KONFIGURASI DAN INISIALISASI ---

st.set_page_config(page_title="Aplikasi Performance Review PT. Bhinneka Rahsa Nusantara", page_icon="📊", layout="wide")

# --- INISIALISASI FIREBASE (LOGIKA BARU YANG LEBIH ROBUST) ---
try:
    # Coba inisialisasi hanya jika belum ada
    firebase_admin.get_app()
except ValueError:
    try:
        # --- PERBAIKAN UTAMA DI SINI ---
        # Ambil kredensial dari secrets.toml. Objek ini bersifat read-only.
        creds_from_secrets = st.secrets["firebase_credentials"]
        
        # Buat salinan yang bisa diubah (mutable copy) dalam bentuk dictionary
        creds_dict = dict(creds_from_secrets)

        # Perbaiki format private_key di dalam salinan dictionary
        if 'private_key' in creds_dict:
            creds_dict['private_key'] = creds_dict['private_key'].replace('\\n', '\n')

        # Inisialisasi Firebase menggunakan dictionary yang sudah diperbaiki
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)

    except Exception as e:
        st.error("Gagal menginisialisasi Firebase. Pastikan `[firebase_credentials]` ada dan formatnya benar di secrets.toml Anda.")
        st.error(f"Detail Error: {e}")
        st.stop()


# --- PERUBAHAN: Konfigurasi Gemini API dari config.py ---
generation_model = None
embedding_model = None # Disiapkan sesuai permintaan

if not API_KEY or API_KEY == "MASUKKAN_API_KEY_ANDA_DI_SINI":
    st.warning("API Key Gemini belum diatur di config.py. Fitur rangkuman AI tidak akan tersedia.", icon="⚠️")
else:
    try:
        genai.configure(api_key=API_KEY)
        generation_model = genai.GenerativeModel('gemini-2.5-flash')
        embedding_model = genai.GenerativeModel('models/embedding-001') # Tidak digunakan, tapi didefinisikan
    except Exception as e:
        st.error(f"Gagal mengkonfigurasi Gemini API: {e}")
        st.stop()

db = firestore.client()

if 'user_info' not in st.session_state:
    st.session_state.user_info = None
if 'gemini_summary' not in st.session_state:
    st.session_state.gemini_summary = None

# --- FUNGSI-FUNGSI BANTUAN ---

def register_user(employee_type, data):
    """Mendaftarkan pengguna baru ke Auth dan Firestore."""
    username = data['username']
    password = data['password']
    
    users_ref = db.collection('users').where(filter=FieldFilter('username', '==', username)).limit(1).stream()
    if len(list(users_ref)) > 0:
        st.error(f"Username '{username}' sudah digunakan.")
        return False
        
    try:
        dummy_email = f"{username.lower().replace(' ', '_')}@performance.review"
        user = auth.create_user(email=dummy_email, password=password, display_name=data['nama'])

        firestore_data = { 
            'uid': user.uid, 'employee_id': data['employee_id'], 'nama': data['nama'], 
            'username': username, 'email': dummy_email, 'job_position': data['job_position'], 
            'tipe_karyawan': employee_type, 'app_feedback_submitted': False # Untuk fitur feedback
        }
        
        if employee_type == 'office':
            firestore_data['organization'] = data['organization']
            firestore_data['job_level'] = data['job_level']

        db.collection('users').document(user.uid).set(firestore_data)
        st.success(f"Registrasi untuk '{username}' berhasil!")
        return True

    except Exception as e:
        st.error(f"Registrasi gagal: {e}")
        try: auth.delete_user(user.uid)
        except Exception: pass
        return False

def get_user_details(uid):
    try:
        user_doc = db.collection('users').document(uid).get()
        return user_doc.to_dict() if user_doc.exists else None
    except Exception as e: return None

def get_assigned_reviewees(reviewer_uid):
    try:
        assignments_ref = db.collection('review_assignments').where(filter=FieldFilter('reviewer_uid', '==', reviewer_uid)).stream()
        reviewee_ids = [doc.to_dict()['reviewee_uid'] for doc in assignments_ref]
        reviewee_details = {uid: get_user_details(uid).get('nama', f"UID: {uid}") for uid in reviewee_ids if get_user_details(uid)}
        return reviewee_details
    except Exception as e: return {}

def get_reviewed_uids(reviewer_uid):
    """Mengambil set UID dari reviewee yang sudah direview oleh reviewer."""
    try:
        reviews_ref = db.collection('reviews').where(filter=FieldFilter('reviewer_uid', '==', reviewer_uid)).stream()
        return {doc.to_dict().get('reviewee_uid') for doc in reviews_ref}
    except Exception as e:
        st.error(f"Gagal memuat data review: {e}")
        return set()

def get_review_questions(employee_type):
    try:
        doc = db.collection('review_questions').document(employee_type).get()
        return doc.to_dict().get('questions', []) if doc.exists else []
    except Exception as e: return []

def update_review_questions(employee_type, questions_list):
    try:
        doc_ref = db.collection('review_questions').document(employee_type)
        doc_ref.set({'questions': questions_list})
        st.success(f"Daftar pertanyaan untuk tipe '{employee_type}' berhasil diperbarui.")
        return True
    except Exception as e:
        st.error(f"Gagal memperbarui pertanyaan: {e}")
        return False

def submit_review(reviewer_uid, reviewee_uid, responses):
    try:
        review_data = {'reviewer_uid': reviewer_uid, 'reviewee_uid': reviewee_uid, 'responses': responses, 'timestamp': firestore.SERVER_TIMESTAMP}
        db.collection('reviews').add(review_data)
        return True
    except Exception as e: 
        st.error(f"Gagal mengirim review: {e}")
        return False

def get_my_reviews(reviewee_uid):
    try:
        reviews_ref = db.collection('reviews').where(filter=FieldFilter('reviewee_uid', '==', reviewee_uid)).stream()
        return [review.to_dict() for review in reviews_ref]
    except Exception as e: return []

def has_user_submitted_feedback(uid):
    """Mengecek apakah user sudah pernah submit feedback aplikasi."""
    user_details = get_user_details(uid)
    return user_details.get('app_feedback_submitted', False)

@firestore.transactional
def submit_app_feedback_transaction(transaction, uid, user_nama, rating, suggestion):
    """Menyimpan feedback dan update status user dalam satu transaksi."""
    feedback_ref = db.collection('app_feedback').document()
    transaction.set(feedback_ref, {
        'user_uid': uid,
        'user_nama': user_nama,
        'ease_of_use_rating': rating,
        'suggestion': suggestion,
        'timestamp': firestore.SERVER_TIMESTAMP
    })
    user_ref = db.collection('users').document(uid)
    transaction.update(user_ref, {'app_feedback_submitted': True})

def process_app_feedback_submission(uid, user_nama, rating, suggestion):
    """Wrapper untuk memanggil transaksi."""
    try:
        transaction = db.transaction()
        submit_app_feedback_transaction(transaction, uid, user_nama, rating, suggestion)
        st.success("Terima kasih! Ulasan Anda telah berhasil dikirim.")
        return True
    except Exception as e:
        st.error(f"Terjadi kesalahan saat mengirim ulasan: {e}")
        return False

def generate_summary_with_gemini(all_comments):
    """Fungsi untuk memanggil Gemini AI dan membuat rangkuman."""
    # PERUBAHAN: Memastikan model sudah dikonfigurasi sebelum digunakan
    if not generation_model:
        st.error("Model AI tidak berhasil dikonfigurasi. Tidak dapat membuat rangkuman.")
        return None
    
    prompt = f"""
    Anda adalah seorang asisten HR yang profesional dan suportif. Tugas Anda adalah menganalisis data performance review seorang karyawan dan membuat rangkuman yang konstruktif dalam Bahasa Indonesia.

    Berikut adalah kumpulan masukan kualitatif (komentar dan saran) dari beberapa penilai:
    ---
    {all_comments}
    ---

    Berdasarkan data di atas, buatlah sebuah rangkuman dalam format berikut:

    ### Ringkasan Umum
    Satu paragraf singkat yang merangkum keseluruhan feedback secara netral dan seimbang.

    ### Kekuatan Utama (Strengths)
    Buat dalam bentuk poin-poin (bullet points), identifikasi 2-3 kekuatan utama yang paling sering disebut atau paling menonjol dari semua komentar.

    ### Area untuk Pengembangan (Areas for Improvement)
    Buat dalam bentuk poin-poin (bullet points), identifikasi 2-3 area pengembangan yang paling sering disebut dari komentar dan saran.

    ### Saran Langkah Berikutnya
    Berikan satu paragraf saran umum yang positif dan memotivasi untuk pengembangan karyawan ke depannya.

    Gunakan bahasa yang positif, profesional, dan membangun. Fokus pada pertumbuhan dan pengembangan, bukan pada kelemahan.
    """
    
    try:
        # PERUBAHAN: Menggunakan model yang sudah dikonfigurasi secara global
        response = generation_model.generate_content(prompt)
        return response.text
    except Exception as e:
        st.error(f"Gagal menghasilkan rangkuman dari AI: {e}")
        return None

def get_all_users():
    """Mengambil semua pengguna dari koleksi 'users'."""
    try:
        users_ref = db.collection('users').stream()
        return {user.id: user.to_dict().get('nama', 'Tanpa Nama') for user in users_ref}
    except Exception as e:
        st.error(f"Gagal mengambil daftar pengguna: {e}")
        return {}

def get_all_assignments(assignment_type):
    """Mengambil semua penugasan yang ada berdasarkan tipe."""
    try:
        assignments_ref = db.collection('review_assignments').where(filter=FieldFilter('assignment_type', '==', assignment_type)).stream()
        assignments_list = []
        all_users = get_all_users()
        for doc in assignments_ref:
            data = doc.to_dict()
            reviewer_name = all_users.get(data.get('reviewer_uid'), 'Pengguna Dihapus')
            reviewee_name = all_users.get(data.get('reviewee_uid'), 'Pengguna Dihapus')
            assignments_list.append({
                'id': doc.id,
                'reviewer_name': reviewer_name,
                'reviewee_name': reviewee_name
            })
        return assignments_list
    except Exception as e:
        st.error(f"Gagal mengambil daftar penugasan: {e}")
        return []

def add_assignment(reviewer_uid, reviewee_uid, assignment_type):
    """Menambahkan penugasan baru dengan tipe dan memeriksa duplikat."""
    try:
        existing_ref = db.collection('review_assignments').where(filter=FieldFilter('reviewer_uid', '==', reviewer_uid)).where(filter=FieldFilter('reviewee_uid', '==', reviewee_uid)).where(filter=FieldFilter('assignment_type', '==', assignment_type)).limit(1).stream()
        if len(list(existing_ref)) > 0:
            st.warning("Penugasan ini sudah ada.")
            return False
        
        db.collection('review_assignments').add({
            'reviewer_uid': reviewer_uid,
            'reviewee_uid': reviewee_uid,
            'assignment_type': assignment_type
        })
        st.success("Penugasan berhasil ditambahkan.")
        return True
    except Exception as e:
        st.error(f"Gagal menambahkan penugasan: {e}")
        return False

def delete_assignment(assignment_id):
    """Menghapus penugasan berdasarkan ID dokumennya."""
    try:
        db.collection('review_assignments').document(assignment_id).delete()
        st.success("Penugasan berhasil dihapus.")
        return True
    except Exception as e:
        st.error(f"Gagal menghapus penugasan: {e}")
        return False

# --- TAMPILAN APLIKASI ---

if st.session_state.user_info is None:
    st.title("Selamat Datang di Aplikasi Performance Review Rahsa Nusantara")
    
    login_tab, register_tab = st.tabs(["🔐 Login", "✍️ Registrasi Karyawan Baru"])

    with login_tab:
        st.markdown("""
        Berikut adalah Performance Review untuk periode **1 Januari 2025 - 30 Juni 2025** dan pelaksanaan penilaian akan dilakukan pada tanggal **1 - 10 Juli 2025**.

        **Catatan:**
        Silahkan login menggunakan Username dan Password yang sudah dibagikan ke email masing-masing:
        - **Username:** Nama Lengkap Anda
        - **Password:** *Unique code* yang sudah dibagikan ke email

        Apabila ada hal yang ingin ditanyakan, silahkan hubungi PnC melalui e-mail maupun WhatsApp. Terima kasih.

        *Regards,*
        
        *People and Culture*
        """)
        st.divider()
        
        st.subheader("Login ke Akun Anda")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Login"):
                if username and password:
                    try:
                        # Logika login disederhanakan untuk contoh
                        users_ref = db.collection('users').where(filter=FieldFilter('username', '==', username)).limit(1).stream()
                        if not (user_docs := list(users_ref)):
                            st.error("Username tidak ditemukan atau password salah.")
                        else:
                            user_data = user_docs[0].to_dict()
                            # Di aplikasi nyata, verifikasi password harus dilakukan di sisi server
                            st.session_state.user_info = { 
                                "uid": user_data.get('uid'), 
                                "email": user_data.get('email'), 
                                "username": user_data.get('username'), 
                                "nama": user_data.get('nama') 
                            }
                            st.rerun()
                    except Exception as e: st.error(f"Login Gagal: Terjadi kesalahan sistem.")
                else: st.warning("Username dan password tidak boleh kosong.")
                
    with register_tab:
        st.subheader("Formulir Pendaftaran")
        reg_type = st.radio("Pilih Tipe Karyawan:", ("Office", "Operator"), horizontal=True, key="reg_type")
        if reg_type == "Office":
            with st.form("register_office_form"):
                st.markdown("**Formulir Karyawan Office**")
                employee_id = st.text_input("Employee ID"); nama = st.text_input("Nama Karyawan (akan menjadi Username)"); organization = st.text_input("Organization"); job_position = st.text_input("Job Position"); job_level = st.text_input("Job Level"); unique_code = st.text_input("Unique Code (akan menjadi Password)", type="password")
                if st.form_submit_button("Daftarkan Karyawan Office"):
                    if all([employee_id, nama, organization, job_position, job_level, unique_code]):
                        data = {'employee_id': employee_id, 'nama': nama, 'username': nama, 'organization': organization, 'job_position': job_position, 'job_level': job_level, 'password': unique_code}
                        register_user('office', data)
                    else: st.warning("Harap isi semua field.")
        elif reg_type == "Operator":
            with st.form("register_operator_form"):
                st.markdown("**Formulir Karyawan Operator**")
                employee_id = st.text_input("Employee ID"); nama = st.text_input("Nama Karyawan (akan digunakan sebagai Username)"); job_position = st.text_input("Job Position"); unique_code = st.text_input("Kode Unik (akan digunakan sebagai Password)", type="password")
                if st.form_submit_button("Daftarkan Karyawan Operator"):
                    if all([employee_id, nama, job_position, unique_code]):
                        data = {'employee_id': employee_id, 'nama': nama, 'username': nama, 'job_position': job_position, 'password': unique_code}
                        register_user('operator', data)
                    else: st.warning("Harap isi semua field.")
else:
    # --- DASHBOARD SETELAH LOGIN ---
    user_info = st.session_state.user_info
    is_admin = user_info.get('username') == 'Data Rahsa'
    
    with st.sidebar:
        welcome_name = user_info.get('nama', user_info.get('username'))
        st.markdown(f"Selamat datang, **{welcome_name}**")
        st.divider()
        menu_options = ["📝 Beri Review", "📊 Lihat Hasil Saya", "⭐ Beri Ulasan Aplikasi"]
        if is_admin:
            menu_options.append("⚙️ Panel Admin")
        app_mode = st.radio("Menu Navigasi", menu_options)
        st.divider()
        if st.button("Logout", use_container_width=True):
            st.session_state.user_info = None; st.rerun()

    if app_mode == "📝 Beri Review":
        st.title("📝 Dashboard Performance Review")
        st.info("Nama-nama karyawan di dropdown adalah rekan kerja yang perlu teman-teman beri penilaian. Silakan pilih nama yang tersedia untuk memberikan penilaian. Nama yang sudah dinilai akan otomatis hilang dari daftar.")
        reviewer_uid = user_info['uid']
        all_assigned_reviewees = get_assigned_reviewees(reviewer_uid)
        reviewed_uids = get_reviewed_uids(reviewer_uid)
        pending_reviewees = {uid: name for uid, name in all_assigned_reviewees.items() if uid not in reviewed_uids}

        if not pending_reviewees:
            st.success("✅ Anda telah menyelesaikan semua review yang ditugaskan. Terima kasih atas partisipasi Anda!")
        else:
            selected_reviewee_uid = st.selectbox("Pilih Karyawan untuk Dinilai:", options=list(pending_reviewees.keys()), format_func=lambda uid: pending_reviewees[uid], index=None, placeholder="Pilih nama karyawan...")
            
            if selected_reviewee_uid:
                reviewee_details = get_user_details(selected_reviewee_uid)
                employee_type = reviewee_details.get('tipe_karyawan') if reviewee_details else None
                if not employee_type:
                    st.error("Tipe karyawan tidak ditemukan.")
                else:
                    st.divider()
                    st.header(f"Formulir untuk: {pending_reviewees[selected_reviewee_uid]} ({employee_type.capitalize()})")
                    
                    questions = get_review_questions(employee_type)
                    if questions:
                        with st.form("review_form"):
                            
                            # --- BAGIAN I: PENILAIAN KUANTITATIF (TERGANTUNG TIPE) ---
                            if employee_type == 'office':
                                st.info(
                                    """
                                    **Before you fill the performance scoring session, please keep in mind that this is a scale-based score. The scale interpretation as mentioned below:**
                                    
                                    **Sebelum mengisi sesi penilaian performa, mohon diingat bahwa ini merupakan penilaian berbasis skala. Skala yang digunakan memiliki interpretasi sebagai berikut:**
                                    
                                    ---
                                    - **1 = high-improvement needed** (*perlu banyak pengembangan*)
                                    - **2 = small-improvement needed** (*masih perlu pengembangan*)
                                    - **3 = target achieved** (*memenuhi target*)
                                    - **4 = more than achieved** (*memenuhi diatas target*)
                                    - **5 = excellently achieved** (*sangat melebihi target*)
                                    ---
                                    Guidelines lebih lengkap mengenai skala penilaian dapat dicek di [bit.ly/RN_PRGuidelines](https://bit.ly/RN_PRGuidelines)
                                    Teman-teman diharapkan dapat menilai dengan menjawab pertanyaan dengan se-objektif mungkin dan sesuai dengan keadaan sebenar-benarnya. Informasi mengenai hal ini bersifat *confidential* akan di-keep oleh tim PnC dan dijamin kerahasiaannya.
                                    """
                                )
                                st.markdown(f"**Bagian I: Penilaian Kuantitatif**")
                                responses = {}
                                for i, q in enumerate(questions):
                                    parts = q.split('|') if '|' in q else [q, '']
                                    question_label = f"**{parts[0].strip()}**<br><small>{parts[1].strip()}</small>"
                                    st.markdown(question_label, unsafe_allow_html=True)
                                    score = st.slider(f"slider_{i}", 1, 5, 3, key=f"q_{i}", label_visibility="collapsed")
                                    responses[q] = score
                                    st.divider()
                            
                            elif employee_type == 'operator':
                                st.info("Teman-teman diharapkan dapat menilai dengan menjawab pertanyaan dengan se-objektif mungkin dan sesuai dengan keadaan sebenar-benarnya.")
                                st.markdown(f"**Bagian I: Penilaian Kuantitatif**")
                                selections = {} 
                                for i, q in enumerate(questions):
                                    parts = q.split(';')
                                    if len(parts) >= 4:
                                        question_text = parts[0].strip()
                                        options = [p.strip() for p in parts[1:4]]
                                        st.markdown(f"**{question_text}**")
                                        selections[q] = st.radio(f"radio_{i}", options, index=None, key=f"q_{i}", label_visibility="collapsed")
                                        st.divider()
                            
                            # --- BAGIAN II: PENILAIAN KUALITATIF (TERGANTUNG TIPE) ---
                            st.markdown(f"**Bagian II: Penilaian Kualitatif**")

                            # --- PERUBAHAN 1: Logika kondisional untuk Komentar & Saran Pengembangan ---
                            if employee_type == 'office':
                                st.markdown("##### Comment (Komentar) (Wajib Diisi)")
                                comment = st.text_area("comment_office", label_visibility="collapsed")
                                st.caption("""
                                Gunakan kerangka di bawah:

                                • I Like : kekuatan & dampaknya (contoh proyek/tanggal)
                                • I Wonder : area yang masih bisa ditajamkan (kenapa)
                                • I Wish : harapan konkret 3-6 bulan ke depan
                                Min. 120 karakter 
                                • Fokus pada perilaku, bukan pribadi
                                """)
                                
                                st.markdown("##### Saran Pengembangan (Wajib Diisi)")
                                dev_suggestion = st.text_area("dev_suggestion_office", label_visibility="collapsed")
                                st.caption("""
                                Tulis apa–bagaimana–kapan.

                                1️⃣ Skill Prioritas (hard/soft) 
                                2️⃣ Metode (training, mentoring, proyek rotasi) 
                                
                                Contoh: “Pelatihan Power BI.”
                                """)
                            
                            elif employee_type == 'operator':
                                st.markdown("##### Komentar (Wajib Diisi)")
                                comment = st.text_area("comment_operator", label_visibility="collapsed")
                                st.caption("""
                                Silakan masukan beberapa catatan yang perlu untuk diketahui karyawan ini, boleh menggunakan metode apa yang saya suka dari karyawan ini, apa yang saya pikir baik untuk karyawan ini jika ia lakukan/miliki, dan apa yang saya pikir karyawan ini harus lakukan/miliki dalam kolom komentar operator. 
                                
                                *Contoh: Karyawan ini sangat positif dalam bekerja baik secara individu maupun dalam tim. Ia menyelesaikan pekerjaannya dengan cepat dan berkualitas. Akan lebih baik jika karyawan ini bisa lebih menguasai tentang metode-metode yang mendukung pekerjaannya.*
                                """)
                                dev_suggestion = None # Tidak ada saran pengembangan untuk operator

                            # --- Tombol & Logika Submit ---
                            if st.form_submit_button("Kirim Review"):
                                all_quantitative_answered = True
                                if employee_type == 'office':
                                    # 'responses' sudah terisi oleh slider
                                    pass
                                elif employee_type == 'operator':
                                    responses = {}
                                    for q, selection in selections.items():
                                        if selection is None:
                                            all_quantitative_answered = False
                                            break
                                        options_list = [p.strip() for p in q.split(';')[1:4]]
                                        responses[q] = options_list.index(selection) + 1
                                
                                # --- PERUBAHAN 2: Validasi input yang disesuaikan ---
                                validation_passed = True
                                if not all_quantitative_answered:
                                    st.error("Mohon jawab semua pertanyaan pada Bagian I (Penilaian Kuantitatif).")
                                    validation_passed = False
                                
                                if not comment:
                                    st.error("Mohon isi bagian Komentar. Kolom ini wajib diisi.")
                                    validation_passed = False
                                
                                if employee_type == 'office' and not dev_suggestion:
                                    st.error("Mohon isi bagian Saran Pengembangan. Kolom ini wajib diisi.")
                                    validation_passed = False

                                if validation_passed:
                                    responses['Komentar'] = comment
                                    if employee_type == 'office':
                                        responses['Saran Pengembangan'] = dev_suggestion
                                    
                                    if submit_review(reviewer_uid, selected_reviewee_uid, responses):
                                        st.toast("Review berhasil dikirim! ✅")
                                        time.sleep(1)
                                        st.rerun()
    
    elif app_mode == "📊 Lihat Hasil Saya":
        st.title("📊 Hasil Performance Review Anda")
        my_reviews = get_my_reviews(user_info['uid'])
        if not my_reviews:
            st.info("Belum ada hasil review yang tersedia untuk Anda.")
        else:
            st.markdown(f"Anda telah menerima **{len(my_reviews)}** penilaian. Berikut adalah rinciannya:")
            user_details = get_user_details(user_info['uid'])
            employee_type = user_details.get('tipe_karyawan') if user_details else None

            my_reviews.sort(key=lambda r: r.get('timestamp', pd.Timestamp.min), reverse=True)
            
            # --- PERBAIKAN: Inisialisasi di luar loop ---
            question_scores = {}
            total_scores_list = []
            all_comments_text = ""

            for i, review in enumerate(my_reviews):
                review_date = review.get('timestamp', 'N/A')
                if hasattr(review_date, 'strftime'):
                    review_date = review_date.strftime('%d %B %Y, %H:%M')
                    
                with st.expander(f"**Penilaian ke-{i + 1}** (Diterima pada: `{review_date}`)", expanded=(i==0)):
                    scores = {k: v for k, v in review.get('responses', {}).items() if isinstance(v, (int, float))}
                    comments = {k: v for k, v in review.get('responses', {}).items() if isinstance(v, str)}
                    
                    if scores:
                        st.subheader("Penilaian Kuantitatif")
                        for question, score in scores.items():
                            if question not in question_scores:
                                question_scores[question] = []
                            question_scores[question].append(score)
                            total_scores_list.append(score)

                            if employee_type == 'operator' and ';' in question:
                                parts = question.split(';')
                                if len(parts) > int(score):
                                    question_text = parts[0].strip()
                                    answer_text = parts[int(score)].strip()
                                    st.markdown(f"**{question_text}**")
                                    st.info(f"Jawaban: {answer_text}")
                                else:
                                    st.markdown(f"**{question}** (Data tidak lengkap)")
                            
                            else: # Untuk tipe Office
                                question_display = question.split('|')[0].strip() if '|' in question else question
                                max_value = 5
                                st.markdown(f"**{question_display}**")
                                st.progress(score / max_value)
                                st.caption(f"Skor: {score}/{max_value}")
                            st.markdown("---")
                    
                    if comments:
                        st.subheader("Masukan Kualitatif")
                        comment_text = comments.get('Komentar') or comments.get('Komentar Umum')
                        if comment_text:
                            st.markdown("**Comment (Komentar)**")
                            st.info(comment_text)
                            all_comments_text += f"- Komentar: {comment_text}\n"
                        if 'Saran Pengembangan' in comments:
                            st.markdown("**Saran Pengembangan**")
                            st.info(comments['Saran Pengembangan'])
                            all_comments_text += f"- Saran Pengembangan: {comments['Saran Pengembangan']}\n"
                        all_comments_text += "---\n"

            st.divider()
            st.header("Ringkasan dan Rata-Rata Penilaian")

            if total_scores_list:
                max_value = 5 if employee_type == 'office' else 3
                overall_average = sum(total_scores_list) / len(total_scores_list)
                st.metric(label="Rata-Rata Nilai Keseluruhan", value=f"{overall_average:.2f} / {max_value}")
                st.progress(overall_average / max_value)
                st.markdown("---")

                st.subheader("Rincian Rata-Rata per Item Pertanyaan")
                for question, scores_list in question_scores.items():
                    avg_score = sum(scores_list) / len(scores_list)
                    
                    if employee_type == 'operator' and ';' in question:
                        question_display = question.split(';')[0].strip()
                    elif '|' in question:
                        question_display = f"**{question.split('|')[0].strip()}**<br><small>{question.split('|')[1].strip()}</small>"
                    else:
                        question_display = question
                    
                    st.markdown(question_display, unsafe_allow_html=True)
                    st.text(f"Rata-rata Skor: {avg_score:.2f}")
                    st.divider()
            else:
                st.info("Tidak ada data penilaian kuantitatif untuk dihitung rata-ratanya.")
            
            # --- BAGIAN BARU: Tombol Generate Rangkuman AI ---
            st.header("Analisis Rangkuman dengan AI")
            
            if not generation_model:
                st.warning("Fitur rangkuman AI tidak tersedia. Mohon atur API Key Anda di file `config.py`.", icon="🔒")
            elif st.button("✨ Buat Rangkuman dengan AI"):
                with st.spinner("AI sedang menganalisis dan membuat rangkuman... Ini mungkin memerlukan beberapa saat."):
                    summary = generate_summary_with_gemini(all_comments_text)
                    st.session_state.gemini_summary = summary
            
            if st.session_state.gemini_summary:
                st.markdown(st.session_state.gemini_summary)

    elif app_mode == "⭐ Beri Ulasan Aplikasi":
        st.title("⭐ Ulasan Penggunaan Aplikasi")
        st.markdown("Kami sangat menghargai masukan Anda untuk membuat platform ini lebih baik lagi di masa mendatang.")
        st.divider()

        if has_user_submitted_feedback(user_info['uid']):
            st.success("✅ Terima kasih! Anda sudah pernah memberikan ulasan untuk aplikasi ini.")
            st.info("Setiap pengguna hanya dapat memberikan ulasan sebanyak satu kali.")
        else:
            with st.form("app_feedback_form"):
                st.subheader("Seberapa mudah penggunaan platform ini untuk performance review?")
                ease_of_use = st.radio(
                    "Pilih salah satu:",
                    options=["4 - Sangat Mudah", "3 - Mudah", "2 - Agak Sulit", "1 - Sangat Sulit"],
                    index=None, label_visibility="collapsed"
                )
                st.subheader("Apakah ada saran untuk pelaksanaan Performance Review berikutnya?")
                suggestion = st.text_area("Saran Anda (opsional)", placeholder="Tulis saran Anda di sini...")
                submitted = st.form_submit_button("Kirim Ulasan")
                if submitted:
                    if not ease_of_use:
                        st.warning("Mohon pilih tingkat kemudahan penggunaan platform.")
                    else:
                        rating_value = int(ease_of_use.split(" - ")[0])
                        if process_app_feedback_submission(user_info['uid'], user_info['nama'], rating_value, suggestion):
                            time.sleep(1)
                            st.rerun()

    elif app_mode == "⚙️ Panel Admin" and is_admin:
        st.title("⚙️ Panel Admin")
        admin_tab1, admin_tab2 = st.tabs(["📝 Kelola Pertanyaan Review", "🔗 Kelola Penugasan"])
        with admin_tab1:
            st.header("Kelola Pertanyaan Performance Review")
            q_type = st.selectbox("Pilih tipe karyawan untuk dikelola:", ("office", "operator"), key="q_type")
            st.warning("""
            **Penting: Aturan Format Pertanyaan**

            **Untuk Tipe Office (Bilingual):**
            - Gunakan pemisah `|` (garis vertikal).
            - Format: `Pertanyaan Bahasa Inggris | Pertanyaan Bahasa Indonesia`

            **Untuk Tipe Operator (Pilihan Ganda Deskriptif):**
            - Gunakan pemisah `;` (titik koma).
            - Format: `Pertanyaan;Pilihan untuk skor 1;Pilihan untuk skor 2;Pilihan untuk skor 3`
            """)
            current_questions = get_review_questions(q_type)
            questions_text = "\n".join(current_questions)
            st.markdown(f"**Edit pertanyaan untuk tipe `{q_type}` di bawah ini (satu pertanyaan per baris):**")
            new_questions_text = st.text_area("Daftar Pertanyaan:", value=questions_text, height=400, key=f"questions_{q_type}")
            if st.button("Simpan Perubahan Pertanyaan", key=f"save_{q_type}"):
                updated_questions_list = [line.strip() for line in new_questions_text.split("\n") if line.strip()]
                update_review_questions(q_type, updated_questions_list)
        
        with admin_tab2:
            st.header("Kelola Penugasan Reviewer")
            # ... (Logika Panel Admin tidak berubah)
            assignment_type_to_manage = st.radio("Pilih tipe penugasan untuk dikelola:", ("office", "operator"), horizontal=True, key="assignment_type")
            all_users = get_all_users()
            user_names = list(all_users.values())
            user_uids = list(all_users.keys())
            with st.form("add_assignment_form"):
                st.subheader(f"Tambah Penugasan Baru untuk Tipe: `{assignment_type_to_manage.capitalize()}`")
                col1, col2 = st.columns(2)
                with col1:
                    reviewer_name = st.selectbox("Pilih Reviewer:", options=user_names, index=None, placeholder="Pilih nama...")
                with col2:
                    reviewee_name = st.selectbox("Pilih Reviewee:", options=user_names, index=None, placeholder="Pilih nama...")
                submitted = st.form_submit_button("Tambahkan Penugasan")
                if submitted:
                    if reviewer_name and reviewee_name:
                        if reviewer_name == reviewee_name:
                            st.error("Reviewer dan Reviewee tidak boleh orang yang sama.")
                        else:
                            reviewer_uid = user_uids[user_names.index(reviewer_name)]
                            reviewee_uid = user_uids[user_names.index(reviewee_name)]
                            if add_assignment(reviewer_uid, reviewee_uid, assignment_type_to_manage):
                                st.rerun() 
                    else:
                        st.warning("Harap pilih Reviewer dan Reviewee.")
            st.divider()
            st.subheader(f"Daftar Penugasan Saat Ini (Tipe: `{assignment_type_to_manage.capitalize()}`)")
            assignments = get_all_assignments(assignment_type_to_manage)
            if not assignments:
                st.info("Belum ada penugasan yang dibuat untuk tipe ini.")
            else:
                for assignment in assignments:
                    col1, col2, col3, col4 = st.columns([3, 1, 3, 1])
                    with col1:
                        st.write(f"**{assignment['reviewer_name']}**")
                    with col2:
                        st.write("➔")
                    with col3:
                        st.write(f"**{assignment['reviewee_name']}**")
                    with col4:
                        if st.button("Hapus", key=f"del_{assignment['id']}", use_container_width=True):
                            delete_assignment(assignment['id'])
                            st.rerun()
