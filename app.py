# app.py
# Jalankan dengan: streamlit run app.py

import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore_v1.base_query import FieldFilter
import pandas as pd

# --- KONFIGURASI DAN INISIALISASI ---

st.set_page_config(page_title="Aplikasi Performance Review", page_icon="�", layout="wide")

# --- INISIALISASI FIREBASE (LOGIKA BARU YANG LEBIH ROBUST) ---
if not firebase_admin._apps:
    try:
        creds_dict = st.secrets["firebase_credentials"]
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
    except Exception as e:
        st.error("Gagal menginisialisasi koneksi ke Firebase. Harap periksa konfigurasi 'Secrets' Anda di Streamlit Cloud.")
        st.stop()

db = firestore.client()

if 'user_info' not in st.session_state:
    st.session_state.user_info = None

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

        firestore_data = { 'uid': user.uid, 'employee_id': data['employee_id'], 'nama': data['nama'], 'username': username, 'email': dummy_email, 'job_position': data['job_position'], 'tipe_karyawan': employee_type }
        
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

def get_review_questions(employee_type):
    try:
        doc = db.collection('review_questions').document(employee_type).get()
        return doc.to_dict().get('questions', []) if doc.exists else []
    except Exception as e: return []

def update_review_questions(employee_type, questions_list):
    """Memperbarui atau membuat daftar pertanyaan di Firestore."""
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
        st.success("Review berhasil dikirim!")
        return True
    except Exception as e: return False

def get_my_reviews(reviewee_uid):
    try:
        reviews_ref = db.collection('reviews').where(filter=FieldFilter('reviewee_uid', '==', reviewee_uid)).stream()
        reviews_list = []
        for review in reviews_ref:
            review_data = review.to_dict()
            reviewer_details = get_user_details(review_data.get('reviewer_uid'))
            review_data['reviewer_name'] = reviewer_details.get('nama', 'Anonim') if reviewer_details else 'Anonim'
            reviews_list.append(review_data)
        return reviews_list
    except Exception as e: return []

# --- TAMPILAN APLIKASI ---

if st.session_state.user_info is None:
    st.title("Selamat Datang di Aplikasi Performance Review")
    
    login_tab, register_tab = st.tabs(["🔐 Login", "✍️ Registrasi Karyawan Baru"])
    # Logika login dan registrasi... (tidak berubah)
    with login_tab:
        st.subheader("Login ke Akun Anda")
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Login"):
                if username and password:
                    try:
                        users_ref = db.collection('users').where(filter=FieldFilter('username', '==', username)).limit(1).stream()
                        user_docs = list(users_ref)
                        if not user_docs:
                            st.error("Username tidak ditemukan.")
                        else:
                            user_data = user_docs[0].to_dict()
                            user = auth.get_user_by_email(user_data.get('email'))
                            st.session_state.user_info = { "uid": user.uid, "email": user.email, "username": user_data.get('username'), "nama": user_data.get('nama', user.email) }
                            st.rerun()
                    except Exception as e: st.error(f"Login Gagal: Password salah atau terjadi kesalahan sistem.")
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
    
    with st.sidebar:
        welcome_name = user_info.get('nama', user_info.get('username'))
        st.markdown(f"Selamat datang, **{welcome_name}**")
        st.divider()
        # --- PENAMBAHAN MENU ADMIN ---
        app_mode = st.radio("Menu Navigasi", ("📝 Beri Review", "📊 Lihat Hasil Saya", "⚙️ Panel Admin"))
        st.divider()
        if st.button("Logout", use_container_width=True):
            st.session_state.user_info = None; st.rerun()

    if app_mode == "📝 Beri Review":
        # Logika beri review... (tidak berubah)
        st.title("📝 Dashboard Performance Review")
        reviewer_uid = user_info['uid']
        reviewees = get_assigned_reviewees(reviewer_uid)
        if not reviewees: st.warning("Saat ini tidak ada reviewee yang ditugaskan untuk Anda.")
        else:
            selected_reviewee_uid = st.selectbox("Pilih Karyawan:", options=list(reviewees.keys()), format_func=lambda uid: reviewees[uid], index=None, placeholder="Pilih nama karyawan...")
            if selected_reviewee_uid:
                reviewee_details = get_user_details(selected_reviewee_uid)
                employee_type = reviewee_details.get('tipe_karyawan') if reviewee_details else None
                if not employee_type: st.error("Tipe karyawan tidak ditemukan.")
                else:
                    st.divider()
                    st.header(f"Formulir untuk: {reviewees[selected_reviewee_uid]} ({employee_type.capitalize()})")
                    questions = get_review_questions(employee_type)
                    if questions:
                        with st.form("review_form", clear_on_submit=True):
                            responses = {q: st.slider(label=q, min_value=1, max_value=10, value=5, key=f"q_{i}") for i, q in enumerate(questions)}
                            general_comments = st.text_area("Komentar Umum (Opsional):", height=150)
                            if st.form_submit_button("Kirim Review"):
                                if general_comments: responses['Komentar Umum'] = general_comments
                                submit_review(reviewer_uid, selected_reviewee_uid, responses)
    
    elif app_mode == "📊 Lihat Hasil Saya":
        # Logika lihat hasil... (tidak berubah)
        st.title("📊 Hasil Performance Review Anda")
        my_reviews = get_my_reviews(user_info['uid'])
        if not my_reviews: st.info("Belum ada hasil review yang tersedia untuk Anda.")
        else:
            st.markdown(f"Anda memiliki **{len(my_reviews)}** review.")
            my_reviews.sort(key=lambda r: r['timestamp'], reverse=True)
            for i, review in enumerate(my_reviews):
                review_date = review['timestamp'].strftime('%d %B %Y, %H:%M')
                with st.expander(f"**Review dari {review['reviewer_name']}** - `{review_date}`", expanded=(i==0)):
                    scores = {k: v for k, v in review['responses'].items() if isinstance(v, (int, float))}
                    comments = {k: v for k, v in review['responses'].items() if isinstance(v, str)}
                    if scores:
                        st.subheader("Penilaian Kuantitatif"); cols = st.columns(3); col_idx = 0
                        for question, score in scores.items():
                            cols[col_idx].metric(label=question, value=f"{score}/10"); col_idx = (col_idx + 1) % 3
                        st.divider()
                    if comments:
                        st.subheader("Komentar dan Masukan")
                        for question, comment in comments.items(): st.markdown(f"**{question}**"); st.info(comment)

    elif app_mode == "⚙️ Panel Admin":
        st.title("⚙️ Panel Admin")
        st.info("Catatan: Saat ini panel ini dapat diakses semua pengguna. Untuk keamanan, tambahkan pemeriksaan peran admin di masa depan.")
        
        admin_tab1, admin_tab2 = st.tabs(["📝 Kelola Pertanyaan Review", "🔗 Kelola Penugasan"])

        with admin_tab1:
            st.header("Kelola Pertanyaan Performance Review")
            q_type = st.selectbox("Pilih tipe karyawan untuk dikelola:", ("office", "operator"), key="q_type")
            
            # Ambil pertanyaan yang ada saat ini
            current_questions = get_review_questions(q_type)
            # Gabungkan menjadi satu string untuk ditampilkan di text_area
            questions_text = "\n".join(current_questions)
            
            st.markdown(f"**Edit pertanyaan untuk tipe `{q_type}` di bawah ini (satu pertanyaan per baris):**")
            new_questions_text = st.text_area("Daftar Pertanyaan:", value=questions_text, height=400, key=f"questions_{q_type}")
            
            if st.button("Simpan Perubahan Pertanyaan", key=f"save_{q_type}"):
                # Pisahkan teks menjadi daftar pertanyaan, abaikan baris kosong
                updated_questions_list = [line.strip() for line in new_questions_text.split("\n") if line.strip()]
                update_review_questions(q_type, updated_questions_list)
        
        with admin_tab2:
            st.header("Kelola Penugasan Reviewer")
            st.write("Fitur ini masih dalam pengembangan.")
�
