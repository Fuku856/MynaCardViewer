"""マイナンバーカード券面ビューア（tkinter GUI）。

画面: 開始（リーダー選択・モード選択・カード検出）→ 照合番号B入力 → 読み取り中 → 結果
モード: 20歳以上・未満の確認 / 年齢の確認 / 選択した情報を表示
入力中・読み取り中にカードが離れる／抜き差しされる／リーダーが外れると、
入力内容を消して自動的に開始画面へ戻る。
"""
import datetime
import os
import queue
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from . import card as mc
from . import ocr
from . import pcsc
from .history import COLUMNS, History, HistoryError, Record, default_dir
from .imaging import PHOTO_FORMATS, TEXT_IMAGE_FORMATS, open_image
from .session import CardMonitor, run_card_task

APP_TITLE = 'マイナカード券面ビューア'
FONT = ('Yu Gothic UI', 12)
FONT_L = ('Yu Gothic UI', 16)
FONT_XL = ('Yu Gothic UI', 22, 'bold')
FONT_HUGE = ('Yu Gothic UI', 54, 'bold')

MSG_CARD_REMOVED = 'カードとの接続が切れたため、処理を中断しました。\n入力内容は消去しました。'
MSG_READER_LOST = 'カードリーダーとの接続が切れたため、処理を中断しました。\n入力内容は消去しました。'
MSG_HISTORY_BROKEN = ('読み取り履歴を復号できませんでした（ファイルの破損・改ざん、または別の Windows ユーザーの履歴）。\n'
                      '履歴画面の「すべて削除」で新しい履歴を作り直せます。')
NOTICE_REVOCATION = ('※J-LIS の電子署名とカードの実在は確認しますが、紛失・廃止の届けが出たカードかどうか（失効）は'
                     '確認していません。顔写真と本人を必ず見比べてください。')
AUTH_OK_COLOR, AUTH_NG_COLOR = '#1b7f3b', '#b00020'

MODE_ADULT, MODE_AGE, MODE_SELECT = 'adult', 'age', 'select'
MODES = [
    (MODE_ADULT, '20歳以上・未満の確認', '顔写真・氏名と、20歳以上か未満かを表示します'),
    (MODE_AGE, '年齢の確認', '顔写真・氏名と、年齢を表示します'),
    (MODE_SELECT, '選択した情報を表示', '下で選んだ情報だけを表示します'),
]
MODE_NAMES = {key: name for key, name, _ in MODES}
ITEMS = [('photo', '顔写真'), ('name', '氏名'), ('addr', '住所'), ('birth', '生年月日'), ('sex', '性別')]
ITEM_NAMES = dict(ITEMS)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        # 文字はポイント指定で拡大率に応じて大きくなるので、ウィンドウと画像も合わせる
        self.ui_scale = max(1.0, float(self.tk.call('tk', 'scaling')) / (96 / 72))
        self.geometry('%dx%d' % (self.px(980), self.px(740)))
        self.minsize(self.px(880), self.px(700))
        self.queue = queue.Queue()
        self.history = History()
        self.monitor = None
        self.reader = None
        self.reader_state = None
        self.phase = 'start'      # start / checking / input / reading / result
        self.token = 0            # 中断した作業スレッドの結果を捨てるための番号
        self.session_count = None  # 入力開始時のカード抜き差し回数
        self.session_mode = None
        self.session_items = ()
        self.images = []          # PhotoImage の参照保持

        container = tk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.pages = {}
        for name, build in (('start', self.build_start), ('input', self.build_input),
                            ('reading', self.build_reading), ('result', self.build_result)):
            frame = tk.Frame(container, padx=24, pady=16)
            frame.grid(row=0, column=0, sticky='nsew')
            build(frame)
            self.pages[name] = frame

        self.show('start')
        self.refresh_readers()
        self.after(100, self.poll_queue)
        self.after(300, self.migrate_history)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

    def migrate_history(self):
        """旧バージョンの平文 CSV 履歴を暗号化形式へ移す。"""
        try:
            n = self.history.migrate_legacy()
        except (HistoryError, OSError, ValueError) as e:
            messagebox.showwarning(APP_TITLE, '旧形式の履歴を暗号化形式へ移せませんでした。\n%s' % e, parent=self)
            return
        if n:
            messagebox.showinfo(APP_TITLE, '読み取り履歴 %d 件を暗号化して保存し直しました。\n'
                                           '暗号化前の履歴ファイルは削除しました。' % n, parent=self)

    def px(self, n):
        return int(n * self.ui_scale)

    # ---------- 画面構築 ----------

    def build_start(self, f):
        tk.Label(f, text=APP_TITLE, font=FONT_XL).pack(pady=(4, 2))
        tk.Label(f, text='マイナンバーカードのICチップから券面の情報を読み取って表示します。\n'
                         '読み取った情報は画面を閉じると消去されます（履歴には一部の項目だけを残します）。',
                 font=FONT, justify=tk.CENTER).pack(pady=(0, 10))

        row = tk.Frame(f)
        row.pack(pady=4)
        tk.Label(row, text='カードリーダー：', font=FONT).pack(side=tk.LEFT)
        self.reader_combo = ttk.Combobox(row, state='readonly', width=40, font=FONT)
        self.reader_combo.pack(side=tk.LEFT)
        self.reader_combo.bind('<<ComboboxSelected>>', lambda e: self.select_reader(self.reader_combo.get()))
        tk.Button(row, text='再検索', font=FONT, command=self.refresh_readers).pack(side=tk.LEFT, padx=8)

        modes = tk.LabelFrame(f, text='確認モード', font=FONT, padx=16, pady=6)
        modes.pack(pady=8, fill=tk.X, padx=60)
        self.mode_var = tk.StringVar(value=MODE_ADULT)
        for key, name, desc in MODES:
            line = tk.Frame(modes)
            line.pack(fill=tk.X)
            tk.Radiobutton(line, text=name, value=key, variable=self.mode_var, font=FONT_L,
                           command=self.on_mode_change).pack(side=tk.LEFT)
            tk.Label(line, text=desc, font=FONT, fg='#555').pack(side=tk.LEFT, padx=10)
        items = tk.Frame(modes)
        items.pack(fill=tk.X, padx=(28, 0), pady=(2, 4))
        self.item_vars = {}
        self.item_checks = []
        for key, name in ITEMS:
            var = tk.BooleanVar(value=True)
            cb = tk.Checkbutton(items, text=name, variable=var, font=FONT, command=self.update_status)
            cb.pack(side=tk.LEFT, padx=6)
            self.item_vars[key] = var
            self.item_checks.append(cb)

        self.status_label = tk.Label(f, text='', font=FONT_L)
        self.status_label.pack(pady=14)
        self.next_button = tk.Button(f, text='次へ', font=FONT_L, width=12, state=tk.DISABLED,
                                     command=self.start_check)
        self.next_button.pack()
        self.on_mode_change()
        tk.Button(f, text='読み取り履歴', font=FONT, command=self.open_history).pack(side=tk.BOTTOM, anchor='e')

    def build_input(self, f):
        tk.Label(f, text='照合番号Bの入力', font=FONT_XL).pack(pady=(10, 4))
        self.input_mode_label = tk.Label(f, text='', font=FONT_L, fg='#0b5394')
        self.input_mode_label.pack()
        tk.Label(f, text='カードの券面を見て入力してください。間違えると残り回数が減り、0回でロックされます。',
                 font=FONT).pack()
        self.remaining_label = tk.Label(f, text='', font=FONT_L, fg='#b00020')
        self.remaining_label.pack(pady=8)

        form = tk.Frame(f)
        form.pack(pady=8)
        fields = [('生年月日（券面の和暦の年2桁＋月日 6桁）', 6, '例: 昭和60年4月12日 → 600412（元年は01）'),
                  ('有効期限の西暦年（4桁）', 4, '例: 2034年4月12日まで有効 → 2034'),
                  ('セキュリティコード（4桁）', 4, '券面に印字された4桁')]
        self.entries = []
        for i, (label, size, hint) in enumerate(fields):
            tk.Label(form, text=label, font=FONT, anchor='e').grid(row=i, column=0, sticky='e', pady=6)
            # exportselection=False: 選択しただけで照合番号がクリップボード（PRIMARY 選択）に渡らないようにする
            e = tk.Entry(form, font=FONT_L, width=size + 2, show='●', exportselection=False)
            e.grid(row=i, column=1, sticky='w', padx=8)
            e.bind('<Return>', lambda ev: self.start_read())
            tk.Label(form, text=hint, font=FONT, fg='#555').grid(row=i, column=2, sticky='w')
            self.entries.append(e)
        self.show_pin = tk.BooleanVar(value=False)
        tk.Checkbutton(form, text='入力内容を表示する', font=FONT, variable=self.show_pin,
                       command=self.toggle_pin).grid(row=3, column=1, columnspan=2, sticky='w', pady=6)
        tk.Label(f, text='生年月日が券面で不明の場合　年が不明: 000000　月が不明: 年2桁+9900　日が不明: 年2桁+月2桁+99',
                 font=FONT, fg='#555').pack()

        buttons = tk.Frame(f)
        buttons.pack(pady=16)
        tk.Button(buttons, text='読み取る', font=FONT_L, width=12, command=self.start_read).pack(side=tk.LEFT, padx=8)
        tk.Button(buttons, text='やめる', font=FONT_L, width=12, command=self.back_to_start).pack(side=tk.LEFT, padx=8)

    def build_reading(self, f):
        tk.Label(f, text='読み取り中…', font=FONT_XL).pack(pady=(120, 10))
        tk.Label(f, text='カードをリーダーから動かさないでください', font=FONT_L).pack()
        self.progress = ttk.Progressbar(f, mode='indeterminate', length=360)
        self.progress.pack(pady=20)

    def build_result(self, f):
        self.result_mode_label = tk.Label(f, text='', font=FONT, fg='#555')
        self.result_mode_label.pack(anchor='w')
        self.result_auth = tk.Label(f, text='', font=FONT_L, fg='white', anchor='w', justify=tk.LEFT,
                                    padx=12, pady=6, wraplength=self.px(900))
        self.result_auth.pack(fill=tk.X, pady=(2, 4))
        self.result_big = tk.Label(f, text='', font=FONT_HUGE)
        self.result_ref = tk.Label(f, text='', font=FONT_L, fg='#555')
        self.result_body = tk.Frame(f)
        self.result_body.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.result_footer = tk.Frame(f)
        self.result_footer.pack(fill=tk.X)
        tk.Label(self.result_footer, text='券面の印字と見比べて確認してください。', font=FONT).pack(pady=(4, 0))
        tk.Label(self.result_footer, text=NOTICE_REVOCATION, font=('Yu Gothic UI', 10), fg='#8a5300',
                 wraplength=self.px(900), justify=tk.LEFT).pack(pady=(2, 0))
        tk.Button(self.result_footer, text='表示を消して最初に戻る', font=FONT_L,
                  command=self.back_to_start).pack(pady=8)

    # ---------- 画面遷移・共通 ----------

    def show(self, name):
        self.phase = name
        self.pages[name].tkraise()
        if name == 'reading':
            self.progress.start(12)
        else:
            self.progress.stop()
        self.update_status()

    def on_mode_change(self):
        state = tk.NORMAL if self.mode_var.get() == MODE_SELECT else tk.DISABLED
        for cb in self.item_checks:
            cb.configure(state=state)
        self.update_status()

    def selected_items(self, mode):
        if mode == MODE_SELECT:
            return tuple(key for key, _ in ITEMS if self.item_vars[key].get())
        return ('photo', 'name')

    def clear_inputs(self):
        for e in self.entries:
            e.delete(0, tk.END)
        self.show_pin.set(False)
        self.toggle_pin()

    def clear_result(self):
        self.images.clear()
        self.result_big.configure(text='')
        self.result_big.pack_forget()
        self.result_ref.configure(text='')
        self.result_ref.pack_forget()
        self.result_auth.configure(text='', bg=self.cget('bg'))
        for child in self.result_body.winfo_children():
            child.destroy()

    def back_to_start(self):
        self.token += 1
        self.session_count = None
        self.clear_inputs()
        self.clear_result()
        self.show('start')

    def abort(self, message):
        """入力中・読み取り中の処理を中断して開始画面に戻る。"""
        if self.phase not in ('checking', 'input', 'reading'):
            return
        self.back_to_start()
        messagebox.showwarning(APP_TITLE, message, parent=self)

    def toggle_pin(self):
        for e in self.entries:
            e.configure(show='' if self.show_pin.get() else '●')

    def on_close(self):
        self.token += 1
        if self.monitor:
            self.monitor.stop()
        self.destroy()

    # ---------- リーダー監視 ----------

    def refresh_readers(self):
        try:
            with pcsc.Context() as ctx:
                readers = ctx.list_readers()
        except pcsc.PCSCError:
            readers = []
        self.reader_combo['values'] = readers
        if not readers:
            self.reader_combo.set('')
            self.select_reader(None)
            return
        if self.reader not in readers:
            preferred = [r for r in readers if any(k in r for k in ('Sony', 'FeliCa', 'PaSoRi', 'NFC'))]
            self.reader_combo.set((preferred or readers)[0])
            self.select_reader(self.reader_combo.get())

    def select_reader(self, reader):
        if reader == self.reader and self.monitor:
            return
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.reader = reader or None
        self.reader_state = None
        if self.phase != 'start':
            self.abort(MSG_READER_LOST)
        if self.reader:
            self.monitor = CardMonitor(self.reader, self.queue)
            self.monitor.start()
        self.update_status()

    def on_reader_state(self, state):
        self.reader_state = state
        if state.lost:
            self.monitor = None
            self.reader = None
            self.abort(MSG_READER_LOST)
            self.after(1000, self.refresh_readers)
        elif self.phase in ('checking', 'input', 'reading'):
            if not state.present or (self.session_count is not None and state.count != self.session_count):
                self.abort(MSG_CARD_REMOVED)
        self.update_status()

    def update_status(self):
        if self.phase != 'start':
            return
        if not self.reader:
            text, ok = 'カードリーダーが見つかりません。接続して「再検索」を押してください', False
        elif self.reader_state is None:
            text, ok = 'カードリーダーを確認しています…', False
        elif not self.reader_state.present:
            text, ok = 'マイナンバーカードをリーダーに置いてください', False
        elif not self.selected_items(self.mode_var.get()):
            text, ok = '表示する情報を1つ以上選んでください', False
        else:
            text, ok = 'カードを検出しました。「次へ」を押してください', True
        self.status_label.configure(text=text)
        self.next_button.configure(state=tk.NORMAL if ok else tk.DISABLED)

    # ---------- カード処理 ----------

    def start_check(self):
        if not (self.reader and self.reader_state and self.reader_state.present):
            return
        mode = self.mode_var.get()
        items = self.selected_items(mode)
        if not items:
            return
        self.session_mode, self.session_items = mode, items
        self.session_count = self.reader_state.count
        self.token += 1
        self.phase = 'checking'
        self.next_button.configure(state=tk.DISABLED)
        self.status_label.configure(text='カードを確認しています…')
        run_card_task(self.reader, mc.pin_b_remaining, self.queue, ('check', self.token))

    def start_read(self):
        if self.phase != 'input':
            return
        try:
            pin = mc.build_pin_b(*(e.get().strip() for e in self.entries))
        except ValueError as e:
            messagebox.showwarning(APP_TITLE, str(e), parent=self)
            return
        token = self.token
        ok = messagebox.askokcancel(APP_TITLE, '入力した照合番号Bで認証します。\n'
                                               '間違っていると残り回数が1回減ります。よろしいですか？',
                                    icon='warning', default='cancel', parent=self)
        if not ok or token != self.token or self.phase != 'input':
            return  # 確認中にカードが離れて中断された場合も含む
        self.token += 1
        self.show('reading')
        items = self.session_items
        run_card_task(self.reader, lambda c: mc.read_visual_info(c, pin), self.queue, ('read', self.token),
                      after=lambda info: (info, self.recognize_for_history(info, items)))

    @staticmethod
    def recognize_for_history(info, items):
        """履歴用に、表示する項目だけ OCR する（カードとの接続を閉じた後に実行）。

        読めなかったときだけ、原因調査用に個人情報を含まない診断ログ（画像形式と認識文字数）を
        履歴と同じフォルダに上書き保存する。読めたときは古い診断ログを消す。
        """
        diag = ['%s' % datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')]
        name = pref = ''
        if 'name' in items:
            diag.append('[name]')
            name = ocr.first_char(ocr.recognize(info.name_png, diag=diag))
            diag.append('  result: %s' % ('ok' if name else 'not recognized'))
        if 'addr' in items:
            diag.append('[addr]')
            pref = ocr.prefecture(ocr.recognize(info.addr_png, accept=lambda t: bool(ocr.prefecture(t)), diag=diag))
            diag.append('  result: %s' % ('ok' if pref else 'not recognized'))
        failed = ('name' in items and not name) or ('addr' in items and not pref)
        path = os.path.join(default_dir(), 'ocr_diag.log')
        try:
            if failed:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(diag) + '\n')
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        return name, pref

    def on_task_result(self, kind, token, payload, is_error):
        if token != self.token:
            return  # 中断済みの処理の結果
        if is_error:
            self.on_task_error(kind, payload)
        elif kind == 'check':
            self.remaining_label.configure(text='照合番号Bの残り回数：%d 回' % payload)
            if payload == 0:
                self.back_to_start()
                messagebox.showerror(APP_TITLE, str(mc.PinLockedError()), parent=self)
                return
            self.input_mode_label.configure(text='モード：%s' % MODE_NAMES[self.session_mode])
            self.clear_inputs()
            self.show('input')
            self.entries[0].focus_set()
        elif kind == 'read':
            info, (name_initial, pref) = payload
            self.clear_inputs()
            self.session_count = None
            self.show_result(info)
            self.save_history(info, name_initial, pref)

    def on_task_error(self, kind, e):
        if isinstance(e, pcsc.ConnectionLostError):
            self.abort(MSG_CARD_REMOVED)
        elif isinstance(e, mc.WrongPinError):
            self.remaining_label.configure(text='照合番号Bの残り回数：%d 回' % e.remaining)
            self.show('input')
            messagebox.showwarning(APP_TITLE, str(e), parent=self)
        else:
            self.back_to_start()
            messagebox.showerror(APP_TITLE, str(e) if isinstance(e, mc.CardError) else
                                 'カードの読み取り中にエラーが発生しました。\n%s' % e, parent=self)

    def poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg[0] == 'state':
                    _, monitor, state = msg
                    if monitor is self.monitor:
                        self.on_reader_state(state)
                else:
                    kind_tag, (kind, token), payload = msg
                    self.on_task_result(kind, token, payload, kind_tag == 'error')
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    # ---------- 結果表示 ----------

    def _image(self, data, formats, max_w, max_h):
        img = open_image(data, formats)
        if img.mode not in ('RGB', 'RGBA', 'L'):
            img = img.convert('RGBA' if 'A' in img.mode else 'RGB')
        img.thumbnail((self.px(max_w), self.px(max_h)), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        self.images.append(photo)
        return photo

    def _image_label(self, parent, data, formats, max_w, max_h, **kw):
        label = tk.Label(parent, **kw)
        if not data:
            label.configure(text='（データなし）', font=FONT_L)
            return label
        try:
            label.configure(image=self._image(data, formats, max_w, max_h))
        except Exception:  # noqa: BLE001 - 画像だけ表示できない場合は他の項目を表示する
            label.configure(text='（画像を表示できません）', font=FONT_L)
        return label

    def big_result(self, info):
        ages = mc.age_range(info.birth, datetime.date.today())
        if self.session_mode == MODE_ADULT:
            if ages is None or (ages[0] < 20 <= ages[1]):
                return '判定できません', '#555'
            return ('20歳以上', '#1b7f3b') if ages[0] >= 20 else ('20歳未満', '#b00020')
        if ages is None:
            return '年齢不明', '#555'
        lo, hi = ages
        return ('%d歳' % lo if lo == hi else '%d〜%d歳' % (lo, hi)), '#0b5394'

    def show_result(self, info: mc.VisualInfo):
        self.clear_result()
        items = self.session_items
        self.result_mode_label.configure(text='モード：%s' % MODE_NAMES[self.session_mode])
        if info.authentic:
            self.result_auth.configure(text='✔ 真正性を確認しました（%s）' % info.auth_message, bg=AUTH_OK_COLOR)
        else:
            self.result_auth.configure(text='✖ 真正性を確認できません：%s\n偽造・改ざんされたカードの可能性があります。'
                                            'この表示を本人確認・年齢確認に使わないでください。' % info.auth_message,
                                       bg=AUTH_NG_COLOR)
        if self.session_mode in (MODE_ADULT, MODE_AGE):
            text, color = self.big_result(info)
            self.result_big.pack(before=self.result_body, pady=(0, 4))
            if info.authentic:
                self.result_big.configure(text=text, fg=color)
            else:
                # 確認できないカードの年齢判定は大きく出さない（参考として小さく表示）
                self.result_big.configure(text='判定できません', fg=AUTH_NG_COLOR)
                self.result_ref.configure(text='（参考：カードの記載では %s）' % text)
                self.result_ref.pack(before=self.result_body)

        body = self.result_body
        photo_h = 300 if self.session_mode != MODE_SELECT else 400
        if 'photo' in items:
            self._image_label(body, info.photo_jp2, PHOTO_FORMATS, int(photo_h * 0.75), photo_h,
                              relief=tk.GROOVE).pack(side=tk.LEFT, anchor='n', padx=(0, 20))
        right = tk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, anchor='n')
        for key, max_h in (('name', 90), ('addr', 140)):
            if key in items:
                tk.Label(right, text=ITEM_NAMES[key], font=FONT, fg='#555', anchor='w').pack(fill=tk.X, pady=(6, 0))
                data = info.name_png if key == 'name' else info.addr_png
                self._image_label(right, data, TEXT_IMAGE_FORMATS, 560, max_h, anchor='w').pack(fill=tk.X)
        texts = (('birth', lambda: mc.format_date(info.birth)), ('sex', lambda: mc.format_sex(info.sex)))
        for key, value in texts:
            if key in items:
                tk.Label(right, text=ITEM_NAMES[key], font=FONT, fg='#555', anchor='w').pack(fill=tk.X, pady=(6, 0))
                tk.Label(right, text=value() or '（データなし）', font=FONT_L, anchor='w').pack(fill=tk.X)
        self.show('result')

    # ---------- 履歴 ----------

    def save_history(self, info, name_initial, pref):
        items = self.session_items
        if self.session_mode == MODE_ADULT:
            kinds = '顔写真・氏名・20歳以上/未満'
        elif self.session_mode == MODE_AGE:
            kinds = '顔写真・氏名・年齢'
        else:
            kinds = '・'.join(ITEM_NAMES[k] for k in items)
        record = Record(
            time=datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
            mode=MODE_NAMES[self.session_mode],
            items=kinds,
            name_initial=(name_initial or '（判読不可）') if 'name' in items else '',
            prefecture=(pref or '（判読不可）') if 'addr' in items else '',
            birth_day=mc.birth_day(info.birth) if 'birth' in items else '',
            authenticity='確認済み' if info.authentic else '確認できず',
        )
        try:
            self.history.append(record)
        except HistoryError:
            messagebox.showwarning(APP_TITLE, '読み取り履歴を保存できませんでした。\n' + MSG_HISTORY_BROKEN, parent=self)
        except OSError as e:
            messagebox.showwarning(APP_TITLE, '読み取り履歴を保存できませんでした。\n%s' % e, parent=self)

    def open_history(self):
        HistoryWindow(self, self.history)


class HistoryWindow(tk.Toplevel):
    def __init__(self, master, history: History):
        super().__init__(master)
        self.title('読み取り履歴')
        self.geometry('%dx%d' % (master.px(1140), master.px(460)))
        self.history = history
        self.transient(master)

        frame = tk.Frame(self, padx=12, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)
        style = ttk.Style(self)
        style.configure('History.Treeview', font=FONT, rowheight=master.px(26))
        style.configure('History.Treeview.Heading', font=FONT)
        widths = [master.px(w) for w in (180, 170, 310, 110, 120, 100, 100)]
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show='headings', style='History.Treeview')
        for col, w in zip(COLUMNS, widths):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor='w')
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)

        buttons = tk.Frame(self, padx=12, pady=(0))
        buttons.pack(fill=tk.X, pady=(0, 12))
        tk.Label(buttons, text='暗号化して保存: %s' % history.path, font=('Yu Gothic UI', 9),
                 fg='#555').pack(side=tk.LEFT)
        tk.Button(buttons, text='閉じる', font=FONT, command=self.destroy).pack(side=tk.RIGHT, padx=4)
        tk.Button(buttons, text='すべて削除', font=FONT, command=self.delete_all).pack(side=tk.RIGHT, padx=4)
        tk.Button(buttons, text='選択した行を削除', font=FONT, command=self.delete_selected).pack(side=tk.RIGHT, padx=4)
        self.reload()

    def reload(self):
        self.tree.delete(*self.tree.get_children())
        try:
            records = self.history.load()
        except HistoryError:
            messagebox.showwarning('読み取り履歴', MSG_HISTORY_BROKEN, parent=self)
            return
        except OSError as e:
            messagebox.showwarning('読み取り履歴', '履歴を読み込めませんでした。\n%s' % e, parent=self)
            return
        for i in reversed(range(len(records))):  # 新しい順
            r = records[i]
            self.tree.insert('', tk.END, iid=str(i),
                             values=(r.time, r.mode, r.items, r.name_initial, r.prefecture, r.birth_day,
                                     r.authenticity))

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        if messagebox.askyesno('読み取り履歴', '選択した %d 件を削除しますか？' % len(sel), parent=self):
            try:
                self.history.delete(int(i) for i in sel)
            except (HistoryError, OSError) as e:
                messagebox.showwarning('読み取り履歴', '削除できませんでした。\n%s' % e, parent=self)
            self.reload()

    def delete_all(self):
        # 復号できない（破損した）履歴もここから作り直せるよう、一覧が空でも実行できる
        if messagebox.askyesno('読み取り履歴', '履歴をすべて削除しますか？\nこの操作は元に戻せません。',
                               icon='warning', parent=self):
            try:
                self.history.clear()
            except OSError as e:
                messagebox.showwarning('読み取り履歴', '削除できませんでした。\n%s' % e, parent=self)
            self.reload()


def main():
    App().mainloop()
