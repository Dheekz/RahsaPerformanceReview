# app.py
# Jalankan dengan: streamlit run app.py

import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, auth
from google.cloud.firestore_v1.base_query import FieldFilter
import pandas as pd

# --- KONFIGURASI DAN INISIALISASI ---

st.set_page_config(page_title="Aplikasi Performance Review", page_icon="📊", layout="wide")

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
        # Tidak perlu mengambil nama reviewer lagi untuk anonimitas
        return [review.to_dict() for review in reviews_ref]
    except Exception as e: return []

# --- FUNGSI BARU UNTUK PANEL ADMIN ---

def get_all_users():
    """Mengambil semua pengguna dari koleksi 'users'."""
    try:
        users_ref = db.collection('users').stream()
        # Mengembalikan dictionary {uid: nama}
        return {user.id: user.to_dict().get('nama', 'Tanpa Nama') for user in users_ref}
    except Exception as e:
        st.error(f"Gagal mengambil daftar pengguna: {e}")
        return {}

def get_all_assignments(assignment_type):
    """Mengambil semua penugasan yang ada berdasarkan tipe."""
    try:
        # Menambahkan filter berdasarkan tipe penugasan
        assignments_ref = db.collection('review_assignments').where(filter=FieldFilter('assignment_type', '==', assignment_type)).stream()
        assignments_list = []
        all_users = get_all_users() # Ambil semua data user sekali saja
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
        # Cek jika penugasan sudah ada
        existing_ref = db.collection('review_assignments').where(filter=FieldFilter('reviewer_uid', '==', reviewer_uid)).where(filter=FieldFilter('reviewee_uid', '==', reviewee_uid)).where(filter=FieldFilter('assignment_type', '==', assignment_type)).limit(1).stream()
        if len(list(existing_ref)) > 0:
            st.warning("Penugasan ini sudah ada.")
            return False
        
        db.collection('review_assignments').add({
            'reviewer_uid': reviewer_uid,
            'reviewee_uid': reviewee_uid,
            'assignment_type': assignment_type # Menyimpan tipe penugasan
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
                        if username == "Data Rahsa" and password == "password123":
                             users_ref = db.collection('users').where(filter=FieldFilter('username', '==', username)).limit(1).stream()
                             if not (user_docs := list(users_ref)):
                                 st.error("Akun admin 'Data Rahsa' tidak ditemukan di database.")
                             else:
                                 user_data = user_docs[0].to_dict()
                                 st.session_state.user_info = { "uid": user_data.get('uid'), "email": user_data.get('email'), "username": user_data.get('username'), "nama": user_data.get('nama') }
                                 st.rerun()
                        else: 
                            users_ref = db.collection('users').where(filter=FieldFilter('username', '==', username)).limit(1).stream()
                            if not (user_docs := list(users_ref)):
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
    
    is_admin = user_info.get('username') == 'Data Rahsa'
    
    with st.sidebar:
        welcome_name = user_info.get('nama', user_info.get('username'))
        st.markdown(f"Selamat datang, **{welcome_name}**")
        st.divider()
        
        menu_options = ["📝 Beri Review", "📊 Lihat Hasil Saya"]
        if is_admin:
            menu_options.append("⚙️ Panel Admin")
        app_mode = st.radio("Menu Navigasi", menu_options)
        
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
        st.title("📊 Hasil Performance Review Anda")
        
        my_reviews = get_my_reviews(user_info['uid'])
        
        if not my_reviews:
            st.info("Belum ada hasil review yang tersedia untuk Anda.")
        else:
            st.markdown(f"Anda telah menerima **{len(my_reviews)}** penilaian. Berikut adalah rinciannya:")
            
            # --- Logika baru untuk agregasi dan tampilan ---
            question_scores = {}
            total_scores_list = []
            
            # Urutkan review dari yang terbaru
            my_reviews.sort(key=lambda r: r['timestamp'], reverse=True)
            
            for i, review in enumerate(my_reviews):
                review_date = review['timestamp'].strftime('%d %B %Y, %H:%M')
                
                # Mengubah judul menjadi anonim
                with st.expander(f"**Penilaian ke-{i + 1}** (Diterima pada: `{review_date}`)", expanded=(i==0)):
                    scores = {k: v for k, v in review['responses'].items() if isinstance(v, (int, float))}
                    comments = {k: v for k, v in review['responses'].items() if isinstance(v, str)}
                    
                    if scores:
                        st.subheader("Penilaian Kuantitatif")
                        # Mengumpulkan skor untuk perhitungan rata-rata
                        for question, score in scores.items():
                            if question not in question_scores:
                                question_scores[question] = []
                            question_scores[question].append(score)
                            total_scores_list.append(score)
                            
                            # Menampilkan pertanyaan dan skor secara vertikal
                            st.markdown(f"<small style='line-height: 1.2;'>{question}</small>", unsafe_allow_html=True)
                            st.progress(score / 10)
                            st.caption(f"Skor: {score}/10")
                            st.markdown("---") # Garis pemisah tipis

                    if comments:
                        st.subheader("Komentar dan Masukan Kualitatif")
                        for question, comment in comments.items():
                            st.markdown(f"**{question}**")
                            st.info(comment)

            st.divider()
            st.header("Ringkasan dan Rata-Rata Penilaian")

            if total_scores_list:
                # Menghitung dan menampilkan rata-rata keseluruhan
                overall_average = sum(total_scores_list) / len(total_scores_list)
                st.metric(label="Rata-Rata Nilai Keseluruhan", value=f"{overall_average:.2f} / 10")
                st.progress(overall_average / 10)
                st.markdown("---")

                st.subheader("Rincian Rata-Rata per Item Pertanyaan")
                # Menghitung dan menampilkan rata-rata per pertanyaan
                for question, scores_list in question_scores.items():
                    avg_score = sum(scores_list) / len(scores_list)
                    st.markdown(f"<small>{question}</small>", unsafe_allow_html=True)
                    st.text(f"Rata-rata Skor: {avg_score:.2f}")
                    st.divider()
            else:
                st.info("Tidak ada data penilaian kuantitatif untuk dihitung rata-ratanya.")


    # --- KONTEN PANEL ADMIN DILINDUNGI ---
    elif app_mode == "⚙️ Panel Admin" and is_admin:
        st.title("⚙️ Panel Admin")
        
        admin_tab1, admin_tab2 = st.tabs(["📝 Kelola Pertanyaan Review", "🔗 Kelola Penugasan"])

        with admin_tab1:
            st.header("Kelola Pertanyaan Performance Review")
            q_type = st.selectbox("Pilih tipe karyawan untuk dikelola:", ("office", "operator"), key="q_type")
            
            current_questions = get_review_questions(q_type)
            questions_text = "\n".join(current_questions)
            
            st.markdown(f"**Edit pertanyaan untuk tipe `{q_type}` di bawah ini (satu pertanyaan per baris):**")
            new_questions_text = st.text_area("Daftar Pertanyaan:", value=questions_text, height=400, key=f"questions_{q_type}")
            
            if st.button("Simpan Perubahan Pertanyaan", key=f"save_{q_type}"):
                updated_questions_list = [line.strip() for line in new_questions_text.split("\n") if line.strip()]
                update_review_questions(q_type, updated_questions_list)
        
        with admin_tab2:
            st.header("Kelola Penugasan Reviewer")
            
            # --- PEMBARUAN: Tambahkan pilihan tipe penugasan ---
            assignment_type_to_manage = st.radio(
                "Pilih tipe penugasan untuk dikelola:",
                ("office", "operator"),
                horizontal=True,
                key="assignment_type"
            )

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
                            # Kirim tipe penugasan ke fungsi
                            if add_assignment(reviewer_uid, reviewee_uid, assignment_type_to_manage):
                                st.rerun() 
                    else:
                        st.warning("Harap pilih Reviewer dan Reviewee.")

            st.divider()
            st.subheader(f"Daftar Penugasan Saat Ini (Tipe: `{assignment_type_to_manage.capitalize()}`)")
            
            # Ambil dan tampilkan daftar berdasarkan tipe yang dipilih
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
