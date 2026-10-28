#!/usr/bin/python3
"""Linux desktop for AURA's existing portable client and central API."""
import json
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_store import LocalStore, LocalStoreError
from employee_sync import sync_employees, inspect_employee_conflict, resolve_employee_conflict, employee_sync_config
from leave_sync import sync_leave, inspect_leave_conflict, accept_server_leave, leave_sync_config, inspect_incoming_leave_conflict, accept_incoming_leave_conflict, LeaveRequestPendingError
from personal_presence_sync import sync_personal_presence
from task_sync import sync_tasks, inspect_task_conflict, resolve_task_conflict, task_sync_config, TaskAssigneePendingError
from subtask_sync import inspect_subtask_conflict, resolve_subtask_conflict
from dependency_sync import inspect_dependency_conflict, resolve_dependency_conflict
from project_sync import sync_projects, inspect_project_conflict, resolve_project_conflict, project_sync_config, ProjectOwnerPendingError
from booking_sync import sync_bookings,inspect_booking_conflict,accept_server_booking_conflict,BookingPendingError
from approval_sync import sync_approvals,inspect_approval_conflict,accept_server_approval,ApprovalPendingError
from maintenance_sync import sync_maintenance, inspect_maintenance_conflict, resolve_maintenance_conflict, maintenance_sync_config, MaintenanceAssetPendingError
from facility_asset_sync import sync_facility_assets, inspect_facility_asset_conflict, resolve_facility_asset_conflict, facility_asset_sync_config
from organization_sync import sync_organizations, inspect_organization_conflict, resolve_organization_conflict, organization_sync_config, OrganizationManagerPendingError
from attachment_sync import sync_attachments, sync_personal_attachments, inspect_attachment_conflict, resolve_attachment_conflict, attachment_sync_config, AttachmentParentPendingError
from ticket_comment_sync import sync_ticket_comments, sync_personal_ticket_comments, ticket_comment_sync_config, CommentTicketPendingError
from ticket_sync import sync_tickets, inspect_ticket_conflict, resolve_ticket_conflict, ticket_sync_config, TicketAssigneePendingError
from task_comment_sync import sync_task_comments, sync_personal_task_comments, task_comment_sync_config, CommentTaskPendingError
from project_assignment_sync import sync_project_assignments, inspect_assignment_conflict, resolve_assignment_conflict, assignment_sync_config, AssignmentParentsPendingError
from room_sync import sync_rooms, inspect_room_conflict, rebase_room_conflict, accept_server_room_conflict, RoomSyncError

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Gio

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / 'KR_BUILD/linux-x64/aura-workspace-enterprise'
CSS = b'''
window, .main { background: #0f172a; color: #f8fafc; }
headerbar, .titlebar { background: #0a0f1e; color: #f8fafc; border-bottom: 1px solid #2d3b58; text-shadow: none; box-shadow: none; }
.sidebar { background: #0a0f1e; border-right: 1px solid #2d3b58; padding: 20px 14px; }
.header { border-bottom: 1px solid #2d3b58; padding: 18px 28px; }
.brand { font-size: 17px; font-weight: bold; color: #f8fafc; }
.subbrand { color: #3b82f6; font-size: 10px; }
.title { font-size: 20px; font-weight: bold; color: #f8fafc; }
.section { font-size: 16px; font-weight: bold; color: #f8fafc; }
.muted { color: #94a3b8; }
.value { font-size: 24px; font-weight: bold; color: #f8fafc; }
.card { background: #182136; border: 1px solid #2d3b58; border-radius: 9px; padding: 16px; }
.item { background: #0f172a; border: 1px solid #1e2941; border-radius: 7px; padding: 12px; }
button { background: #182136; color: #cbd5e1; border: 1px solid #2d3b58; border-radius: 7px; text-shadow: none; box-shadow: none; padding: 8px 12px; }
button:hover { background: #212d4a; color: #f8fafc; }
button.nav { background: transparent; border: 0; }
button.active { background: #1e3a6e; border: 1px solid #3b82f6; color: white; }
button.primary { background: #3b82f6; border-color: #3b82f6; color: white; }
entry, combobox, spinbutton { background: #0f172a; color: #f8fafc; border: 1px solid #2d3b58; }
dialog, dialog box { background: #182136; color: #f8fafc; }
'''
NAV = [('▣  Dashboard','Office Leitstand · Zentrale Übersicht'), ('▤  Räume & Kalender','Ressourcenverwaltung · Räume'), ('✔  Aufgaben & Workflow','Aufgaben-Management · Aktuelle Arbeitsaufträge'), ('○  Team & Präsenz','Mitarbeiter-Verzeichnis & Status'), ('✉  Office Helpdesk','Office-Helpdesk & Facility Support'), ('⚙  Administration','Administration · Verbindung')]

def label(text, style=None):
    w=Gtk.Label(label=str(text)); w.set_xalign(0); w.set_line_wrap(True)
    if style: w.get_style_context().add_class(style)
    return w

def button(text, cb, style=None):
    w=Gtk.Button(label=text); w.connect('clicked',cb)
    if style: w.get_style_context().add_class(style)
    return w

def box(vertical=True, spacing=8, style=None):
    w=Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if style: w.get_style_context().add_class(style)
    return w

def action_flow(actions):
    flow=Gtk.FlowBox();flow.set_selection_mode(Gtk.SelectionMode.NONE);flow.set_homogeneous(False)
    flow.set_column_spacing(8);flow.set_row_spacing(8);flow.set_min_children_per_line(1);flow.set_max_children_per_line(3)
    for widget in actions:flow.add(widget)
    return flow

def room_availability_label(room):
    value=room.get('availability')
    if value in ('free','booked','blocked','unknown'):
        return {'free':'Frei','booked':'Belegt','blocked':'Gesperrt','unknown':'Status unbekannt'}[value]
    return 'Belegt' if room.get('occupied') is True else 'Frei' if room.get('occupied') is False else 'Status unbekannt'

class AuraWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title='AURA Workspace Enterprise · Linux')
        self.personal_task_comment_sync_running=False
        self.personal_attachment_sync_running=False
        self.set_default_size(1200,760); self.set_size_request(850,550)
        self.connect('destroy', Gtk.main_quit)
        self.connect('destroy',self.on_window_destroy)
        self.url=os.environ.get('AURA_SYNC_URL',''); self.token=os.environ.get('AURA_SYNC_TOKEN',''); self.ca=os.environ.get('AURA_SYNC_CA_CERT','')
        self.booking_auto_enabled=False; self.booking_auto_source=0; self.booking_auto_session=None; self.booking_auto_credentials=None; self.booking_auto_error=''; self.booking_auto_interval=60; self.booking_view_generation=None; self.booking_view_room=None;
        self.presence_auto_enabled=False;self.presence_auto_source=0;self.presence_auto_session=None;self.presence_auto_credentials=None;self.presence_auto_error='';self.presence_auto_interval=60;self.presence_auto_notice=None
        self.approval_auto_enabled=False; self.approval_auto_source=0; self.approval_auto_session=None; self.approval_auto_credentials=None; self.approval_auto_error=''; self.approval_auto_interval=60; self.approval_view_generation=None; self.tab=0; self.generation=0; self.local=None; self.local_mode=False; self.local_lock=threading.Lock(); self.room_sync_running=False; self.employee_sync_running=False; self.leave_sync_running=False; self.task_sync_running=False; self.project_sync_running=False; self.ticket_sync_running=False; self.organization_sync_running=False; self.facility_asset_sync_running=False; self.facility_auto={kind:{'enabled':False,'source':0,'retry':0,'running':False,'session':None,'error':'','interval':60,'notice':None} for kind in ('assets','maintenance')}; self.organization_auto_enabled=False; self.organization_auto_source=0; self.organization_auto_retry=0; self.organization_auto_session=None; self.organization_auto_error=''; self.organization_auto_interval=60; self.organization_auto_notice=None; self.attachment_auto={kind:{'enabled':False,'source':0,'retry':0,'running':False,'session':None,'error':'','interval':60,'notice':None} for kind in ('task','ticket')}; self.ticket_auto_waiting=False; self.ticket_comment_auto_waiting=False; self.ticket_auto_enabled=False; self.ticket_auto_source=0; self.ticket_auto_session=None; self.ticket_auto_error=''; self.ticket_auto_interval=60; self.ticket_auto_notice=None; self.ticket_comment_auto_enabled=False; self.ticket_comment_auto_source=0; self.ticket_comment_auto_session=None; self.ticket_comment_auto_error=''; self.ticket_comment_auto_interval=60; self.ticket_comment_auto_notice=None; self.comment_auto_enabled=False; self.comment_auto_source=0; self.comment_auto_session=None; self.comment_auto_error=''; self.comment_auto_interval=60; self.comment_auto_notice=None; self.assignment_auto_enabled=False; self.assignment_auto_source=0; self.assignment_auto_session=None; self.assignment_auto_error=''; self.assignment_auto_interval=60; self.assignment_auto_notice=None; self.project_auto_enabled=False; self.project_auto_source=0; self.project_auto_session=None; self.project_auto_error=''; self.project_auto_interval=60; self.project_auto_notice=None; self.task_auto_enabled=False; self.task_auto_source=0; self.task_auto_session=None; self.task_auto_error=''; self.task_auto_interval=60; self.task_auto_notice=None; self.leave_auto_enabled=False; self.leave_auto_source=0; self.leave_auto_session=None; self.leave_auto_error=''; self.leave_auto_interval=60; self.leave_auto_notice=None; self.employee_auto_enabled=False; self.employee_auto_source=0; self.employee_auto_retry=0; self.employee_auto_session=None; self.employee_auto_error=''; self.employee_auto_interval=60; self.employee_auto_notice=None; self.room_auto_enabled=False; self.room_auto_source=0; self.room_auto_session=None; self.room_auto_error=''; self.room_auto_interval=60; self.room_auto_notice=None; self.window_closed=False
        provider=Gtk.CssProvider(); provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(),provider,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        outer=box(False,0); self.add(outer)
        sidebar=box(True,10,'sidebar'); sidebar.set_size_request(240,-1)
        sidebar_scroll=Gtk.ScrolledWindow();sidebar_scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_propagate_natural_height(False);sidebar_scroll.add(sidebar);outer.pack_start(sidebar_scroll,False,False,0)
        sidebar.pack_start(label('AURA WORKSPACE','brand'),False,False,0)
        sidebar.pack_start(label('ENTERPRISE CORE OS • v2.4.4','subbrand'),False,False,0)
        sidebar.pack_start(label('','muted'),False,False,8)
        self.nav=[]
        for i,(name,_) in enumerate(NAV):
            b=button(name,lambda _b, n=i:self.select(n),'nav'); b.set_halign(Gtk.Align.FILL); sidebar.pack_start(b,False,False,0); self.nav.append(b)
        user_card=box(True,2,'card');self.user_name=label('Kein Benutzer');self.user_role=label('Sitzung wählen','muted')
        user_card.pack_start(self.user_name,False,False,0);user_card.pack_start(self.user_role,False,False,0);sidebar.pack_end(user_card,False,False,0)
        sidebar.pack_end(button('⚙  Serververbindung',lambda _:self.connection()),False,False,0)
        sidebar.pack_end(button('▣  Lokale Sitzung',lambda _:self.local_login()),False,False,0)
        self.state=label('Nicht verbunden','muted'); sidebar.pack_end(self.state,False,False,8)
        main=box(True,0,'main'); outer.pack_start(main,True,True,0)
        header=box(False,8,'header'); main.pack_start(header,False,False,0)
        self.heading=label('','title'); header.pack_start(self.heading,True,True,0)
        header.pack_end(button('↻  Aktualisieren',lambda _:self.refresh()),False,False,0)
        self.admin_expanded={}
        self.own_presence=None;self.presence_revision=0
        self.presence_button=button('Präsenz wird geladen …',lambda _:self.advance_own_presence())
        self.presence_button.set_no_show_all(True);self.presence_button.hide();header.pack_end(self.presence_button,False,False,0)
        scroll=Gtk.ScrolledWindow(); scroll.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC); main.pack_start(scroll,True,True,0)
        self.content=box(True,16); self.content.set_margin_top(24); self.content.set_margin_bottom(24); self.content.set_margin_start(30); self.content.set_margin_end(30); scroll.add(self.content)
        self.room_search_text=''; self.room_search_widget=None
        self.dashboard_source=GLib.timeout_add_seconds(30,self.dashboard_tick)
        self.select(0)

    def dashboard_tick(self):
        if self.window_closed:self.dashboard_source=0;return False
        searching=self.tab==1 and self.room_search_widget is not None and self.room_search_widget.has_focus()
        if self.tab in (0,1) and not searching and (self.local_mode or (self.url and self.token)) and not any(isinstance(window,Gtk.Dialog) and window.get_visible() and window.get_transient_for() is self for window in Gtk.Window.list_toplevels()):self.refresh()
        return True

    def select(self,n):
        self.tab=n; self.heading.set_text(('Lokal · ' if self.local_mode else '')+NAV[n][1])
        for i,b in enumerate(self.nav):
            c=b.get_style_context(); c.add_class('active') if i==n else c.remove_class('active')
        self.refresh()

    def connection(self):
        fields=[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)]
        result=self.form('Serververbindung',fields,'Verbinden')
        if result is not None:
            self.stop_room_auto_sync();self.stop_booking_auto_sync();self.stop_employee_auto_sync();self.stop_presence_auto_sync(clear=True);self.stop_leave_auto_sync();self.stop_approval_auto_sync();self.stop_task_auto_sync();self.stop_project_auto_sync();self.stop_assignment_auto_sync();self.stop_comment_auto_sync();self.stop_ticket_auto_sync();self.stop_ticket_comment_auto_sync();self.stop_attachment_auto_sync('task');self.stop_attachment_auto_sync('ticket');self.stop_organization_auto_sync();self.stop_facility_auto_sync('assets');self.stop_facility_auto_sync('maintenance')
            self.url,self.token,self.ca=result
            with self.local_lock:
                self.local_mode=False
                if self.local: self.local.close(); self.local=None
            self.select(self.tab)

    def local_login(self):
        root=Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'aura-workspace'
        path=Path(os.environ.get('AURA_LOCAL_DB',str(root/'workspace.db')))
        try:
            store=LocalStore(path)
            if not path.exists():
                fields=[('Anzeigename','',False),('Benutzername','',False),
                        ('Neues Passwort (mindestens 12 Zeichen)','',True),('Passwort wiederholen','',True)]
                values=self.form('Lokales Administratorkonto anlegen',fields,'Anlegen')
                if values is None:return
                if not values[0] or not values[1] or values[2]!=values[3] or len(values[2])<12:
                    self.message('Bitte Namen und übereinstimmende Passwörter mit mindestens 12 Zeichen eingeben.');return
                path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                store.create(values[0],values[1],values[2])
            store.open()
            values=self.form('Lokale Anmeldung',[('Benutzername','',False),('Passwort','',True)],'Anmelden')
            if values is None: store.close(); return
            must_change=store.login(values[0],values[1])
            if must_change:
                change=self.form('Passwort ändern',[('Neues Passwort','',True),('Passwort wiederholen','',True)],'Ändern')
                if change is None: store.close(); return
                if len(change[0])<12 or change[0]!=change[1]:
                    store.close(); self.message('Passwörter müssen übereinstimmen und mindestens 12 Zeichen enthalten.'); return
                store.change_password(values[1],change[0])
            self.stop_room_auto_sync();self.stop_booking_auto_sync();self.stop_employee_auto_sync();self.stop_presence_auto_sync(clear=True);self.stop_leave_auto_sync();self.stop_approval_auto_sync();self.stop_task_auto_sync();self.stop_project_auto_sync();self.stop_assignment_auto_sync();self.stop_comment_auto_sync();self.stop_ticket_auto_sync();self.stop_ticket_comment_auto_sync();self.stop_attachment_auto_sync('task');self.stop_attachment_auto_sync('ticket');self.stop_organization_auto_sync();self.stop_facility_auto_sync('assets');self.stop_facility_auto_sync('maintenance')
            with self.local_lock:
                if self.local: self.local.close()
                self.local=store; self.local_mode=True
            self.select(1)
        except (LocalStoreError,OSError) as error:
            if 'store' in locals(): store.close()
            self.message(str(error))

    def form(self,title,fields,submit='Speichern',role_options=None,role_selected=''):
        d=Gtk.Dialog(title=title,transient_for=self,modal=True); d.add_button('Abbrechen',Gtk.ResponseType.CANCEL); d.add_button(submit,Gtk.ResponseType.OK)
        content=d.get_content_area(); content.set_spacing(8); content.set_border_width(20); entries=[]
        for name,value,secret in fields:
            content.pack_start(label(name),False,False,0); e=Gtk.Entry(); e.set_text(str(value)); e.set_visibility(not secret)
            is_person=(not secret and (name.startswith('Mitarbeiter-ID') or name.startswith('Verantwortliche Mitarbeiter-ID') or name.startswith('Leitungs-ID') or name.startswith('Serverkonto-ID der ')))
            if is_person:
                row=box(False,8);row.pack_start(e,True,True,0);row.pack_start(button('Person auswählen',lambda _b,entry=e,allow_none='0 = ' in name:self.choose_form_employee(d,entry,allow_none)),False,False,0);content.pack_start(row,False,False,0)
            elif not secret and name.startswith('Priorität'):
                row=box(False,8);row.pack_start(e,True,True,0);row.pack_start(button('Priorität auswählen',lambda _b,entry=e:self.choose_form_priority(d,entry)),False,False,0);content.pack_start(row,False,False,0)
            elif not secret and name=='Status (open, in_progress, done, cancelled)':
                row=box(False,8);row.pack_start(e,True,True,0);row.pack_start(button('Status auswählen',lambda _b,entry=e:self.choose_form_subtask_status(d,entry)),False,False,0);content.pack_start(row,False,False,0)
            elif not secret and name.startswith('Projekt-ID'):
                row=box(False,8);row.pack_start(e,True,True,0);row.pack_start(button('Projekt auswählen',lambda _b,entry=e:self.choose_form_project(d,entry)),False,False,0);content.pack_start(row,False,False,0)
            else:content.pack_start(e,False,False,0)
            entries.append(e)
        combo=None
        if role_options is not None:
            content.pack_start(label('Systemrolle'),False,False,0);combo=Gtk.ComboBoxText();combo.append('','Keine Änderung' if role_selected else 'Keine Zuweisung')
            for role in role_options:combo.append(role['code'],role['name'])
            if not combo.set_active_id(role_selected):combo.set_active_id('')
            content.pack_start(combo,False,False,0)
        d.set_default_size(450,-1); d.show_all(); response=d.run(); values=[entry.get_text() if field[2] else entry.get_text().strip() for entry,field in zip(entries,fields)]
        if combo is not None:values.append(combo.get_active_id() or '')
        d.destroy()
        return values if response==Gtk.ResponseType.OK else None

    def choose_form_priority(self,parent,entry):
        dialog=Gtk.Dialog(title='Priorität auswählen',transient_for=parent,modal=True);dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übernehmen',Gtk.ResponseType.OK)
        content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8);combo=Gtk.ComboBoxText()
        for value in ('Niedrig','Mittel','Hoch','Kritisch'):combo.append(value,value)
        if not combo.set_active_id(entry.get_text().strip()):combo.set_active_id('Mittel')
        content.pack_start(combo,False,False,0);dialog.show_all();response=dialog.run();selected=combo.get_active_id();dialog.destroy()
        if response==Gtk.ResponseType.OK and selected is not None and parent.get_visible():entry.set_text(selected)

    def choose_form_subtask_status(self,parent,entry):
        dialog=Gtk.Dialog(title='Unteraufgabenstatus auswählen',transient_for=parent,modal=True);dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übernehmen',Gtk.ResponseType.OK)
        content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8);combo=Gtk.ComboBoxText()
        for code,name in (('open','Offen'),('in_progress','In Bearbeitung'),('done','Erledigt'),('cancelled','Storniert')):combo.append(code,name)
        if not combo.set_active_id(entry.get_text().strip()):combo.set_active_id('open')
        content.pack_start(combo,False,False,0);dialog.show_all();response=dialog.run();selected=combo.get_active_id();dialog.destroy()
        if response==Gtk.ResponseType.OK and selected is not None and parent.get_visible():entry.set_text(selected)

    def choose_form_employee(self,parent,entry,allow_none=False):
        mode=self.local_mode;session=self.local;connection=(self.url,self.token,self.ca);generation=self.generation
        def loaded(employees,error):
            if not parent.get_visible() or self.generation!=generation or self.local_mode!=mode:return
            if mode and self.local is not session:return
            if not mode and (self.url,self.token,self.ca)!=connection:return
            def problem(message):
                d=Gtk.MessageDialog(transient_for=parent,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Personenauswahl')
                d.format_secondary_text(message);d.run();d.destroy()
            if error:problem('Mitarbeiterverzeichnis konnte nicht geladen werden: '+str(error));return
            if mode and parent.get_title()=='Konto anlegen':
                def accounts_loaded(accounts,account_error):
                    if not parent.get_visible() or self.generation!=generation or not self.local_mode or self.local is not session:return
                    if account_error:problem('Konten konnten nicht geladen werden: '+str(account_error));return
                    assigned={account['employeeId'] for account in accounts or []}
                    show([employee for employee in employees or [] if employee.get('id') not in assigned])
                self.run_client(['-action','accounts'],accounts_loaded);return
            show(employees)
        def show(employees):
            def problem(message):
                d=Gtk.MessageDialog(transient_for=parent,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Personenauswahl')
                d.format_secondary_text(message);d.run();d.destroy()
            options=[]
            for employee in employees or []:
                person_id=employee.get('id') if mode else employee.get('accountEmployeeId')
                if isinstance(person_id,int) and person_id>0:options.append((person_id,employee))
            if not options and not allow_none:
                problem(('Keine aktiven Mitarbeiter ohne Konto vorhanden.' if parent.get_title()=='Konto anlegen' else 'Keine auswählbaren Mitarbeiter vorhanden.') if mode else 'Keine Mitarbeiter mit zugeordnetem Serverkonto vorhanden. Die ID kann weiterhin direkt eingegeben werden.');return
            options.sort(key=lambda item:(item[1].get('name','').casefold(),item[0]))
            dialog=Gtk.Dialog(title='Person auswählen',transient_for=parent,modal=True);dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übernehmen',Gtk.ResponseType.OK)
            content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8);content.pack_start(label('Aktive Mitarbeiter'),False,False,0)
            combo=Gtk.ComboBoxText()
            if allow_none:combo.append('0','Nicht zugewiesen')
            for person_id,employee in options:
                combo.append(str(person_id),str(employee.get('name',''))+' · '+str(employee.get('department',''))+' · ID '+str(person_id))
            if not combo.set_active_id(entry.get_text().strip()):combo.set_active(0)
            content.pack_start(combo,False,False,0);dialog.show_all();response=dialog.run();selected=combo.get_active_id();dialog.destroy()
            if response==Gtk.ResponseType.OK and selected is not None and parent.get_visible():entry.set_text(selected)
        self.run_client(['-action','employees'],loaded)

    def choose_form_project(self,parent,entry):
        session=self.local;mode=self.local_mode;connection=(self.url,self.token,self.ca);generation=self.generation
        def loaded(projects,error):
            if not parent.get_visible() or self.generation!=generation or self.local_mode!=mode or (mode and self.local is not session) or (not mode and (self.url,self.token,self.ca)!=connection):return
            if error:
                dialog=Gtk.MessageDialog(transient_for=parent,modal=True,message_type=Gtk.MessageType.ERROR,buttons=Gtk.ButtonsType.CLOSE,text='Projektauswahl');dialog.format_secondary_text(str(error));dialog.run();dialog.destroy();return
            dialog=Gtk.Dialog(title='Projekt auswählen',transient_for=parent,modal=True);dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übernehmen',Gtk.ResponseType.OK)
            content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8);content.pack_start(label('Aktive Projekte'),False,False,0)
            combo=Gtk.ComboBoxText();combo.append('0','Keine Zuordnung')
            for project in sorted((p for p in projects or [] if p.get('active',True)),key=lambda p:(p.get('name','').casefold(),p['id'])):
                combo.append(str(project['id']),str(project.get('name',''))+' · '+str(project.get('department',''))+' · ID '+str(project['id']))
            if not combo.set_active_id(entry.get_text().strip()):combo.set_active(0)
            content.pack_start(combo,False,False,0);dialog.show_all();response=dialog.run();selected=combo.get_active_id();dialog.destroy()
            if response==Gtk.ResponseType.OK and selected is not None and parent.get_visible():entry.set_text(selected)
        self.run_client(['-action','projects-for-assignment' if mode else 'projects'],loaded)

    def message(self,text):
        d=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.ERROR,buttons=Gtk.ButtonsType.CLOSE,text='AURA'); d.format_secondary_text(str(text)); d.run(); d.destroy()

    def run_client(self,args,callback):
        if self.local_mode:
            session=self.local
            def local_work():
                try:
                    command=args[args.index('-action')+1]
                    options={args[i]:args[i+1] for i in range(0,len(args)-1,2)}
                    with self.local_lock:
                        if not session or self.local is not session: raise LocalStoreError('Lokale Sitzung wurde gewechselt.')
                        if command in ('rooms','rooms-availability'): result=session.rooms()
                        elif command=='rooms-import': result={'count':session.import_rooms(options['-path'])}
                        elif command=='rooms-export': session.export_rooms(options['-path']); result={}
                        elif command in ('create-room','update-room'):
                            session.save_room(options.get('-id',0),options.get('-name',''),options.get('-location',''),options.get('-capacity',0),options.get('-equipment','')); result={}
                        elif command=='archive-room': session.archive_room(options['-id']); result={}
                        elif command=='tasks': result=session.tasks()
                        elif command=='projects': result=session.projects()
                        elif command=='project-status': session.project_status(options['-id'],options['-expected-status'],options['-status']); result={}
                        elif command=='tickets': result=session.tickets()
                        elif command=='employees': result=session.employees()
                        elif command=='identity': result=session.identity()
                        elif command=='organizations': result=session.organizations()
                        elif command=='organization-save': session.save_organization(options.get('-id',0),options.get('-name',''),options.get('-code',''),options.get('-manager-id',0)); result={}
                        elif command=='organization-archive': session.archive_organization(options['-id']); result={}
                        elif command=='approvals': result=session.approvals()
                        elif command=='approval-create': session.create_approval(options['-task-id'],options['-approver-id']); result={}
                        elif command=='approval-decision':
                            if options['-status'] not in ('approved','rejected'):raise LocalStoreError('Ungültige Entscheidung.')
                            session.decide_approval(options['-id'],options['-status']=='approved'); result={} 
                        elif command=='roles': result=session.roles()
                        elif command=='role-save': session.save_role(options.get('-id',0),options.get('-code',''),options.get('-name',''),options.get('-description',''),options.get('-permissions','')); result={}
                        elif command=='role-archive': session.archive_role(options['-id']); result={}
                        elif command=='audit': result=session.audit(options.get('-limit',200))
                        elif command=='employee-resolution-history': result=session.employee_resolution_history()
                        elif command=='room-resolution-history': result=session.room_resolution_history()
                        elif command=='sync-resolution-history': result=session.sync_resolution_history()
                        elif command=='backup': session.backup(options['-path']); result={}
                        elif command=='restore': result={'previous':session.restore(options['-path'])}
                        elif command=='assign-role': session.assign_role(options['-id'],options['-role']); result={}
                        elif command=='accounts': result=session.accounts()
                        elif command=='account-create': session.create_account(options['-id'],options['-username'],options['-password']); result={}
                        elif command=='account-reset': session.reset_account(options['-id'],options['-password']); result={}
                        elif command=='account-enable': session.enable_account(options['-id'],options['-enabled']=='1'); result={}
                        elif command=='password-change': session.change_password(options['-old'],options['-new']); result={} 
                        elif command in ('create-employee','update-employee'):
                            if options.get('-system-role'):session.save_employee_with_role(options.get('-id',0),options.get('-name',''),options.get('-role',''),options.get('-department',''),options['-system-role'])
                            else:session.save_employee(options.get('-id',0),options.get('-name',''),options.get('-role',''),options.get('-department',''))
                            result={}
                        elif command=='employee-presence': session.employee_presence(options['-id'],options['-presence']); result={}
                        elif command=='archive-employee': session.archive_employee(options['-id']); result={}
                        elif command=='leave': result=session.leave()
                        elif command=='create-leave': session.create_leave(options['-starts-at'],options['-ends-at'],options['-reason']); result={}
                        elif command=='leave-decision':
                            if options['-status'] not in ('approved','rejected'): raise LocalStoreError('Ungültige Entscheidung.')
                            session.decide_leave_compared(options['-id'],options['-status']=='approved',json.loads(options['-payload'])) if options.get('-payload') else session.decide_leave(options['-id'],options['-status']=='approved'); result={} 
                        elif command=='assets': result=session.assets()
                        elif command=='asset-save': session.save_asset(options.get('-id',0),options.get('-name',''),options.get('-category',''),options.get('-location',''),options.get('-status','operational')); result={}
                        elif command=='asset-archive': session.archive_asset(options['-id']); result={}
                        elif command=='maintenance': result=session.maintenance(options['-asset-id'])
                        elif command=='maintenance-save': session.save_maintenance(options['-asset-id'],options.get('-id',0),options.get('-title',''),options.get('-status','open'),options.get('-due',0),options.get('-recurrence',0)); result={}
                        elif command=='maintenance-status': session.maintenance_status(options['-asset-id'],options['-id'],options['-status']); result={}
                        elif command=='maintenance-archive': session.archive_maintenance(options['-asset-id'],options['-id']); result={} 
                        elif command in ('create-ticket','update-ticket'):
                            session.save_ticket(options.get('-id',0),options.get('-title',''),options.get('-category',''),options.get('-priority',''),options.get('-assignee-id',0)); result={}
                        elif command=='ticket-status': session.ticket_status(options['-id'],options['-status']); result={}
                        elif command=='archive-ticket': session.archive_ticket(options['-id']); result={}
                        elif command=='attachments': result=session.attachments(options['-subject'],options['-id'],options.get('-include-archived')=='1')
                        elif command=='attachment-import': session.import_attachment(options['-subject'],options['-id'],options['-path']); result={}
                        elif command=='attachment-verify': result={'path':session.verify_attachment(options['-subject'],options['-id'],options['-attachment-id'])}
                        elif command=='attachment-archive': session.archive_attachment(options['-subject'],options['-id'],options['-attachment-id']); result={}
                        elif command=='attachment-restore': session.restore_attachment(options['-subject'],options['-id'],options['-attachment-id']); result={}
                        elif command=='comments': result=session.comments(options['-subject'],options['-id'])
                        elif command=='comment-add': session.add_comment(options['-subject'],options['-id'],options['-body']); result={}
                        elif command in ('create-task','update-task'):
                            if command=='create-task' and int(options.get('-project-id',0))>0:session.create_task_in_project(options.get('-title',''),options.get('-department',''),options.get('-priority',''),options.get('-assignee-id',0),options.get('-due',0),options.get('-recurrence',0),options['-project-id'])
                            elif command=='update-task' and '-expected-project-id' in options:session.update_task_project(options.get('-id',0),options.get('-title',''),options.get('-department',''),options.get('-priority',''),options.get('-assignee-id',0),options.get('-due',0),options.get('-recurrence',0),options['-expected-project-id'],options['-project-id'])
                            else:session.save_task(options.get('-id',0),options.get('-title',''),options.get('-department',''),options.get('-priority',''),options.get('-assignee-id',0),options.get('-due',0),options.get('-recurrence',0))
                            result={}
                        elif command=='task-status': session.task_status(options['-id'],options['-status']); result={}
                        elif command=='archive-task': session.archive_task(options['-id']); result={}
                        elif command=='assign-project': session.assign_project(options['-id'],options['-project-id']); result={}
                        elif command=='project-edge': session.set_project_edge(options['-id'],options['-project-id'],int(options['-expected']),int(options['-present'])); result={}
                        elif command=='projects-for-assignment': result=session.projects_for_assignment()
                        elif command=='dependencies': result=session.dependencies(options['-task-id'])
                        elif command=='dependency-add': session.dependency_change(options['-task-id'],options['-dependency-id'],True); result={}
                        elif command=='dependency-remove': session.dependency_change(options['-task-id'],options['-dependency-id'],False); result={}
                        elif command=='subtasks': result=session.subtasks(options['-task-id'])
                        elif command=='subtask-save':
                            if options.get('-payload'):session.save_subtask_compared(options['-task-id'],options.get('-id',0),options.get('-title',''),options.get('-status','open'),options.get('-assignee-id',0),options.get('-due',0),json.loads(options['-payload']))
                            else:session.save_subtask(options['-task-id'],options.get('-id',0),options.get('-title',''),options.get('-status','open'),options.get('-assignee-id',0),options.get('-due',0))
                            result={}
                        elif command=='subtask-archive':
                            if options.get('-payload'):session.archive_subtask_compared(options['-task-id'],options['-id'],json.loads(options['-payload']))
                            else:session.archive_subtask(options['-task-id'],options['-id'])
                            result={}
                        elif command in ('create-project','update-project'):
                            session.save_project(options.get('-id',0),options.get('-name',''),options.get('-department',''),options.get('-description',''),options.get('-status','planned'),options.get('-owner-id',0)); result={}
                        elif command=='archive-project': session.archive_project(options['-id']); result={}
                        elif command=='bookings': result=session.bookings(options['-room-id'])
                        elif command=='book':
                            session.book(options['-room-id'],options['-start'],options['-end'],options['-attendees'],options['-occurrences'],options['-interval']); result={}
                        elif command=='reschedule':
                            session.reschedule(options['-room-id'],options['-id'],options['-start'],options['-end'],options['-attendees']); result={}
                        elif command=='cancel-booking': session.cancel_booking(options['-room-id'],options['-id']); result={}
                        elif command=='block-room': session.block_room(options['-room-id'],options['-start'],options['-end'],options['-reason']); result={}
                        else: raise LocalStoreError('Diese Funktion ist lokal noch nicht verfügbar.')
                    error=None
                except (LocalStoreError,ValueError,KeyError,OSError) as exc: result=None; error=str(exc)
                GLib.idle_add(callback,result,error)
            threading.Thread(target=local_work,daemon=True).start()
            return
        if not self.url or not self.token:
            self.state.set_text('Nicht verbunden'); callback(None,'Bitte zuerst Server-URL und Token über „Serververbindung“ eingeben.'); return
        if not CLIENT.is_file(): callback(None,'Linux-Client fehlt. Bitte zuerst clients/linux/build.sh ausführen.'); return
        env=os.environ.copy(); env.update({'AURA_SYNC_URL':self.url,'AURA_SYNC_TOKEN':self.token})
        if self.ca: env['AURA_SYNC_CA_CERT']=self.ca
        else: env.pop('AURA_SYNC_CA_CERT',None)
        def work():
            try:
                p=subprocess.run([str(CLIENT),*args],env=env,capture_output=True,text=True,timeout=120)
                result=json.loads(p.stdout) if p.returncode==0 else None
                error=p.stderr.strip() or 'Unbekannter Clientfehler' if p.returncode else None
            except (OSError,ValueError,subprocess.TimeoutExpired) as e: result=None; error=str(e)
            GLib.idle_add(callback,result,error)
        threading.Thread(target=work,daemon=True).start()

    def update_own_presence(self):
        self.presence_revision+=1;revision=self.presence_revision;self.own_presence=None
        mode=self.local_mode;session=self.local;credentials=(self.url,self.token,self.ca)
        self.user_name.set_text('Serverkonto' if not mode and self.url and self.token else 'Kein Benutzer')
        self.user_role.set_text('Mitarbeiterzuordnung wird geladen …' if not mode and self.url and self.token else 'Sitzung wählen')
        if not mode and (not self.url or not self.token):
            self.presence_button.hide();return
        self.presence_button.set_sensitive(False);self.presence_button.set_label('Präsenz wird geladen …');self.presence_button.show()
        def loaded(value,error):
            if revision!=self.presence_revision or self.local_mode!=mode or (mode and self.local is not session) or (not mode and credentials!=(self.url,self.token,self.ca)):return
            own=next((e for e in (value or []) if e.get('isSelf')),None) if mode else value
            if own and not error:
                self.user_name.set_text(own.get('name') or ('Serverkonto' if not mode else 'Unbekannter Benutzer'))
                self.user_role.set_text(own.get('role') or ('Serververbindung' if not mode else 'Keine Funktion angegeben'))
            elif not mode and self.url and self.token:self.user_role.set_text('Mitarbeiterzuordnung nicht verfügbar')
            if error or not own or own.get('presence') not in ('office','remote','meeting','away'):
                self.presence_button.set_label('Präsenz nicht verfügbar');return
            labels={'office':'Im Büro','remote':'Home-Office','meeting':'Im Meeting','away':'Pause'}
            self.own_presence=own;self.presence_button.set_label('● '+labels[own['presence']]);self.presence_button.set_sensitive(True)
        self.run_client(['-action','employees' if mode else 'my-presence'],loaded)

    def advance_own_presence(self):
        own=self.own_presence
        if not own:return
        states=['office','remote','meeting','away'];current=own.get('presence')
        target=states[(states.index(current)+1)%len(states)] if current in states else 'office'
        self.presence_button.set_sensitive(False);mode=self.local_mode;session=self.local;credentials=(self.url,self.token,self.ca)
        def finished(_value,error):
            if self.local_mode!=mode or (mode and self.local is not session) or (not mode and credentials!=(self.url,self.token,self.ca)):return
            if error:self.message(error)
            self.refresh()
        args=['-action','employee-presence','-id',str(own['id']),'-presence',target] if mode else ['-action','set-my-presence','-status',current,'-presence',target]
        self.run_client(args,finished)

    def clear(self):
        self.room_search_widget=None
        for child in self.content.get_children(): self.content.remove(child)
    def render(self): self.content.show_all()
    def refresh(self):
        self.generation+=1; generation=self.generation; self.clear();self.update_own_presence()
        if self.local_mode and self.tab not in (0,1,2,3,4,5):
            self.content.pack_start(label('Dieser Bereich ist in der lokalen Sitzung noch nicht verfügbar. Räume, Mitarbeiter und Urlaubsanträge können mit dem Server abgeglichen werden.','muted'),False,False,0); self.render(); return
        if self.tab==5:
            self.content.pack_start(label('Synchronisation & Verbindung','section'),False,False,0)
            if self.local_mode: self.content.pack_start(label('Lokale Sitzung: Datenbereiche können nach Einrichtung einzeln mit dem Server abgeglichen werden. Jeder Abgleich zeigt seine Voraussetzungen an.','muted'),False,False,0)
            self.content.pack_start(label('Server: '+(self.url or 'nicht konfiguriert'),'muted'),False,False,0)
            self.content.pack_start(button('Serververbindung bearbeiten',lambda _:self.connection()),False,False,0)
            if self.local_mode:
                self.content.pack_start(label('Betriebsverwaltung','section'),False,False,0)
                self.content.pack_start(button('Lokale Konten',lambda _:self.show_accounts()),False,False,0)
                self.content.pack_start(button('Systemrollen',lambda _:self.show_roles()),False,False,0)
                self.content.pack_start(button('Auditprotokoll',lambda _:self.show_audit()),False,False,0)
                self.content.pack_start(button('Eigenes Passwort ändern',lambda _:self.change_own_password()),False,False,0)
                self.content.pack_start(button('Datenbank sichern',lambda _:self.save_database_backup()),False,False,0)
                self.content.pack_start(button('Datenbank wiederherstellen',lambda _:self.restore_database_backup()),False,False,0)
                self.content.pack_start(label('Sicherung und Wiederherstellung betreffen nur die Datenbank; Anhänge müssen separat gesichert und wiederhergestellt werden.','muted'),False,False,0)
                self.content.pack_start(label('Fachbereiche abgleichen','section'),False,False,0)
                sections={};section_headers={}
                for key,title in (('rooms','Räume'),('people','Mitarbeiter & Organisation'),('facility','Facility & Wartung'),('tasks','Aufgaben & Voraussetzungen'),('projects','Projekte & Zuordnungen'),('tickets','Tickets')):
                    expander=Gtk.Expander(label=title);expander.set_expanded(self.admin_expanded.get(key,False))
                    expander.connect('notify::expanded',lambda widget,_param,k=key:self.admin_expanded.__setitem__(k,widget.get_expanded()))
                    content=box(True,8);content.set_margin_start(16);content.set_margin_top(8);content.set_margin_bottom(8);expander.add(content)
                    self.content.pack_start(expander,False,False,0);sections[key]=content;section_headers[key]=(expander,title)
                sections['rooms'].pack_start(button('Lokale Räume synchronisieren',lambda _:self.start_room_sync()),False,False,0)
                sections['people'].pack_start(button('Lokale Mitarbeiter synchronisieren',lambda _:self.start_employee_sync()),False,False,0)
                sections['people'].pack_start(button('Organisationseinheiten synchronisieren',lambda _:self.start_organization_sync()),False,False,0)
                sections['facility'].pack_start(button('Facility-Anlagen synchronisieren',lambda _:self.start_facility_asset_sync()),False,False,0)
                facility_auto=Gtk.CheckButton(label='Facility-Anlagen automatisch alle 60 Sekunden abgleichen');facility_auto.set_active(self.facility_auto['assets']['enabled']);facility_auto.connect('toggled',lambda widget:self.toggle_facility_auto_sync(widget,'assets'));sections['facility'].pack_start(facility_auto,False,False,0)
                self.facility_auto['assets']['notice']=label(self.facility_auto['assets']['error'],'muted');sections['facility'].pack_start(self.facility_auto['assets']['notice'],False,False,0)
                sections['facility'].pack_start(button('Wartungsaufträge synchronisieren',lambda _:self.start_maintenance_sync()),False,False,0)
                facility_auto=Gtk.CheckButton(label='Wartungsaufträge automatisch alle 60 Sekunden abgleichen');facility_auto.set_active(self.facility_auto['maintenance']['enabled']);facility_auto.connect('toggled',lambda widget:self.toggle_facility_auto_sync(widget,'maintenance'));sections['facility'].pack_start(facility_auto,False,False,0)
                self.facility_auto['maintenance']['notice']=label(self.facility_auto['maintenance']['error'],'muted');sections['facility'].pack_start(self.facility_auto['maintenance']['notice'],False,False,0)
                sections['facility'].pack_start(button('Wartungskonflikt prüfen',lambda _:self.inspect_maintenance_sync_conflict()),False,False,0)
                sections['facility'].pack_start(button('Anlagenkonflikt prüfen',lambda _:self.inspect_facility_asset_sync_conflict()),False,False,0)
                organization_auto=Gtk.CheckButton(label='Organisationseinheiten automatisch alle 60 Sekunden abgleichen');organization_auto.set_active(self.organization_auto_enabled);organization_auto.connect('toggled',self.toggle_organization_auto_sync);sections['people'].pack_start(organization_auto,False,False,0)
                self.organization_auto_notice=label(self.organization_auto_error,'muted');sections['people'].pack_start(self.organization_auto_notice,False,False,0)
                sections['people'].pack_start(button('Organisationskonflikt prüfen',lambda _:self.inspect_organization_sync_conflict()),False,False,0)
                sections['tasks'].pack_start(button('Lokale Aufgaben synchronisieren',lambda _:self.start_task_sync()),False,False,0)
                sections['tickets'].pack_start(button('Lokale Tickets synchronisieren',lambda _:self.start_ticket_sync()),False,False,0)
                ticket_auto=Gtk.CheckButton(label='Tickets automatisch alle 60 Sekunden abgleichen');ticket_auto.set_active(self.ticket_auto_enabled);ticket_auto.connect('toggled',self.toggle_ticket_auto_sync);sections['tickets'].pack_start(ticket_auto,False,False,0)
                self.ticket_auto_notice=label(self.ticket_auto_error,'muted');sections['tickets'].pack_start(self.ticket_auto_notice,False,False,0)
                sections['tickets'].pack_start(button('Ticketkommentare synchronisieren',lambda _:self.start_ticket_comment_sync()),False,False,0)
                sections['tickets'].pack_start(button('Ticketanhänge synchronisieren',lambda _:self.start_attachment_sync('ticket')),False,False,0)
                attachment_auto=Gtk.CheckButton(label='Ticketanhänge automatisch alle 60 Sekunden abgleichen');attachment_auto.set_active(self.attachment_auto['ticket']['enabled']);attachment_auto.connect('toggled',lambda widget:self.toggle_attachment_auto_sync(widget,'ticket'));sections['tickets'].pack_start(attachment_auto,False,False,0)
                self.attachment_auto['ticket']['notice']=label(self.attachment_auto['ticket']['error'],'muted');sections['tickets'].pack_start(self.attachment_auto['ticket']['notice'],False,False,0)
                sections['tickets'].pack_start(button('Ticketanhangskonflikt prüfen',lambda _:self.inspect_attachment_sync_conflict('ticket')),False,False,0)
                ticket_comment_auto=Gtk.CheckButton(label='Ticketkommentare automatisch alle 60 Sekunden abgleichen');ticket_comment_auto.set_active(self.ticket_comment_auto_enabled);ticket_comment_auto.connect('toggled',self.toggle_ticket_comment_auto_sync);sections['tickets'].pack_start(ticket_comment_auto,False,False,0)
                self.ticket_comment_auto_notice=label(self.ticket_comment_auto_error,'muted');sections['tickets'].pack_start(self.ticket_comment_auto_notice,False,False,0)
                sections['tickets'].pack_start(button('Ticketkonflikt prüfen',lambda _:self.inspect_ticket_sync_conflict()),False,False,0)
                sections['tasks'].pack_start(button('Aufgabenkommentare synchronisieren',lambda _:self.start_task_comment_sync()),False,False,0)
                sections['tasks'].pack_start(button('Aufgabenanhänge synchronisieren',lambda _:self.start_attachment_sync('task')),False,False,0)
                attachment_auto=Gtk.CheckButton(label='Aufgabenanhänge automatisch alle 60 Sekunden abgleichen');attachment_auto.set_active(self.attachment_auto['task']['enabled']);attachment_auto.connect('toggled',lambda widget:self.toggle_attachment_auto_sync(widget,'task'));sections['tasks'].pack_start(attachment_auto,False,False,0)
                self.attachment_auto['task']['notice']=label(self.attachment_auto['task']['error'],'muted');sections['tasks'].pack_start(self.attachment_auto['task']['notice'],False,False,0)
                sections['tasks'].pack_start(button('Aufgabenanhangskonflikt prüfen',lambda _:self.inspect_attachment_sync_conflict('task')),False,False,0)
                comment_auto=Gtk.CheckButton(label='Aufgabenkommentare automatisch alle 60 Sekunden abgleichen');comment_auto.set_active(self.comment_auto_enabled);comment_auto.connect('toggled',self.toggle_comment_auto_sync);sections['tasks'].pack_start(comment_auto,False,False,0)
                self.comment_auto_notice=label(self.comment_auto_error,'muted');sections['tasks'].pack_start(self.comment_auto_notice,False,False,0)
                sections['projects'].pack_start(button('Lokale Projekte synchronisieren',lambda _:self.start_project_sync()),False,False,0)
                sections['projects'].pack_start(button('Projekt-Aufgaben-Zuordnungen synchronisieren',lambda _:self.start_assignment_sync()),False,False,0)
                sections['projects'].pack_start(button('Projektzuordnungskonflikt prüfen',lambda _:self.inspect_assignment_sync_conflict()),False,False,0)
                assignment_auto=Gtk.CheckButton(label='Projektzuordnungen automatisch alle 60 Sekunden abgleichen');assignment_auto.set_active(self.assignment_auto_enabled);assignment_auto.connect('toggled',self.toggle_assignment_auto_sync);sections['projects'].pack_start(assignment_auto,False,False,0)
                self.assignment_auto_notice=label(self.assignment_auto_error,'muted');sections['projects'].pack_start(self.assignment_auto_notice,False,False,0)
                sections['projects'].pack_start(button('Projektkonflikt prüfen',lambda _:self.inspect_project_sync_conflict()),False,False,0)
                project_auto=Gtk.CheckButton(label='Projekte automatisch alle 60 Sekunden abgleichen');project_auto.set_active(self.project_auto_enabled);project_auto.connect('toggled',self.toggle_project_auto_sync);sections['projects'].pack_start(project_auto,False,False,0)
                self.project_auto_notice=label(self.project_auto_error,'muted');sections['projects'].pack_start(self.project_auto_notice,False,False,0)
                sections['tasks'].pack_start(button('Aufgabenkonflikt prüfen',lambda _:self.inspect_task_sync_conflict()),False,False,0)
                sections['tasks'].pack_start(button('Unteraufgabenkonflikt prüfen',lambda _:self.inspect_subtask_sync_conflict()),False,False,0)
                sections['tasks'].pack_start(button('Voraussetzungskonflikt prüfen',lambda _:self.inspect_dependency_sync_conflict()),False,False,0)
                task_auto=Gtk.CheckButton(label='Aufgaben automatisch alle 60 Sekunden abgleichen');task_auto.set_active(self.task_auto_enabled);task_auto.connect('toggled',self.toggle_task_auto_sync);sections['tasks'].pack_start(task_auto,False,False,0)
                self.task_auto_notice=label(self.task_auto_error,'muted');sections['tasks'].pack_start(self.task_auto_notice,False,False,0)
                sections['people'].pack_start(button('Mitarbeiterkonflikt prüfen',lambda _:self.inspect_employee_sync_conflict()),False,False,0)
                employee_auto=Gtk.CheckButton(label='Mitarbeiter automatisch alle 60 Sekunden abgleichen');employee_auto.set_active(self.employee_auto_enabled);employee_auto.connect('toggled',self.toggle_employee_auto_sync);sections['people'].pack_start(employee_auto,False,False,0)
                self.employee_auto_notice=label(self.employee_auto_error,'muted');sections['people'].pack_start(self.employee_auto_notice,False,False,0)
                auto=Gtk.CheckButton(label='Räume automatisch alle 60 Sekunden abgleichen');auto.set_active(self.room_auto_enabled);auto.connect('toggled',self.toggle_room_auto_sync);sections['rooms'].pack_start(auto,False,False,0)
                self.room_auto_notice=label(self.room_auto_error,'muted');sections['rooms'].pack_start(self.room_auto_notice,False,False,0)
                sections['rooms'].pack_start(button('Raumkonflikt prüfen',lambda _:self.inspect_room_sync_conflict()),False,False,0)
                def update_section_notice(_widget=None,_param=None,*,key):
                    expander,title=section_headers[key]
                    has_notice=any(isinstance(child,Gtk.Label) and bool(child.get_text().strip()) for child in sections[key].get_children())
                    expander.set_label(title+(' · Hinweis' if has_notice else ''))
                for key,content in sections.items():
                    for child in content.get_children():
                        if isinstance(child,Gtk.Label):child.connect('notify::label',lambda widget,param,k=key:update_section_notice(widget,param,key=k))
                    update_section_notice(key=key)
            else:
                self.content.pack_start(button('Zentrale Konten verwalten',lambda _:self.show_managed_accounts()),False,False,0)
                self.content.pack_start(button('Zentrale Rollen verwalten',lambda _:self.show_managed_roles()),False,False,0)
                self.content.pack_start(button('Zentrales Ereignis-Audit',lambda _:self.show_central_event_history()),False,False,0)
                self.content.pack_start(button('Zentrale Datenbank sichern',lambda _:self.show_central_backup()),False,False,0)
                self.content.pack_start(label('Zentrale Konten werden derzeit über die Serveridentitätskonfiguration verwaltet. Eine zentrale Sicherung wird ausschließlich auf dem Server abgelegt. Wiederherstellung erfolgt ausschließlich bei gestopptem Server; lokale Datenbankbefehle gelten nicht für die Zentrale.','muted'),False,False,0)
            self.render(); return
        self.content.pack_start(label('Daten werden geladen …','muted'),False,False,0); self.render()
        actions={0:['rooms-availability','tasks','tickets','employees','identity'],1:['rooms-availability','identity'],2:['identity','tasks','projects'],3:['identity','employees','leave'],4:['identity','tickets']}.get(self.tab,[])
        results={}; errors=[]
        def next_action(index):
            if generation!=self.generation: return
            if index==len(actions):
                self.clear(); self.state.set_text((('Lokale Sitzung · Raumabgleich automatisch' if self.room_auto_enabled else 'Lokale Sitzung · Raumabgleich manuell') if self.local_mode else 'Verbindung aktiv') if not errors else 'Verbindung / Rechte prüfen')
                if errors:
                    self.content.pack_start(label('\n'.join(errors),'muted'),False,False,0)
                self.render_data(results); self.render(); return
            action=actions[index]
            def done(value,error):
                if generation!=self.generation:return
                if error: errors.append(action+': '+error)
                else:
                    key='rooms' if action=='rooms-availability' else action
                    results[key]=[v for v in (value or []) if v.get('active', True)] if key in ('rooms','projects') else (value or [])
                next_action(index+1)
            self.run_client(['-action',action],done)
        next_action(0)

    def render_data(self,data):
        if self.tab==0:
            stats=box(False,16)
            for title,key in [('Aktive Mitarbeiter','employees'),('Freie Meetingräume','rooms'),('Offene Tickets','tickets'),('Offene Aufgaben','tasks')]:
                card=box(True,8,'card'); card.pack_start(label(title,'muted'),False,False,0)
                value=data.get(key)
                if value is None:count='—'
                elif key=='rooms':
                    count=(str(sum(1 for v in value if not v['occupied']))+' von '+str(len(value))) if all(isinstance(v.get('occupied'),bool) for v in value) else '—'
                else:count=str(sum(1 for v in value if key not in ('tasks','tickets') or v.get('status') not in ('done','cancelled','closed','resolved')))
                card.pack_start(label(count,'value'),False,False,0); card.pack_start(label('Lokale Daten' if self.local_mode else 'Zentrale Daten','muted'),False,False,0); stats.pack_start(card,True,True,0)
            self.content.pack_start(stats,False,False,0)
            panels=box(False,16); left=box(True,12,'card'); right=box(True,12,'card')
            left.pack_start(label('Räume & Ressourcen','section'),False,False,0)
            for r in data.get('rooms',[])[:5]:
                occupancy=room_availability_label(r)
                left.pack_start(label(f"{r.get('name')} · {r.get('location')} · {r.get('capacity')} Plätze · {occupancy}"),False,False,0)
            if not data.get('rooms'): left.pack_start(label('Keine Räume verfügbar.','muted'),False,False,0)
            schedule=box(True,8);left.pack_start(schedule,False,False,0)
            self.load_dashboard_schedule(schedule,data.get('rooms'),self.generation)
            right.pack_start(label('Office Board & Quick Actions','section'),False,False,0)
            if self.local_mode:right.pack_start(label('Lokale Änderungen können mit dem Server abgeglichen werden.','muted'),False,False,0)
            identity=data.get('identity') or {};actor=identity.get('employeeId') or ''
            if self.local_mode and identity.get('canManageRoles'):
                audit_note=label('Audit wird geladen …','muted');right.pack_start(audit_note,False,False,0)
                generation=self.generation;session=self.local
                def audit_loaded(entries,error):
                    if self.generation!=generation or self.tab!=0 or not self.local_mode or self.local is not session:return
                    audit_note.set_text('Audit: '+('nicht verfügbar' if error else (entries[0] if entries else 'noch keine Änderungen')))
                self.run_client(['-action','audit','-limit','1'],audit_loaded)
            if identity.get('canRequestLeave'):right.pack_start(button('+ Urlaubsantrag einreichen',lambda _:self.create_leave()),False,False,0)
            if identity.get('canManageTickets'):
                right.pack_start(button('+ Kaffeebohnen-Nachfüllung melden',lambda _:self.create('ticket',{'title':'Kaffeebohnen nachfüllen','category':'Facility','assigneeId':actor})),False,False,0)
                right.pack_start(button('+ Neues Ticket an IT-Support senden',lambda _:self.create('ticket',{'category':'IT-Support','assigneeId':actor})),False,False,0)
            if identity.get('canManageTasks'):right.pack_start(button('+ Aufgabe anlegen',lambda _:self.create('task',{'assigneeId':actor,'canManageProjects':identity.get('canManageProjects')})),False,False,0)
            if identity.get('canManageRooms'):right.pack_start(button('+ Raum anlegen',lambda _:self.create('room')),False,False,0)
            panels.pack_start(left,True,True,0); panels.pack_start(right,True,True,0); self.content.pack_start(panels,True,True,0); return
        if self.tab == 3:
            identity=data.get('identity') or {};can_manage=bool(identity.get('canManageEmployees'));actor=identity.get('employeeId')
            if self.local_mode:
                team=box(False,8);team.pack_start(label('Mitarbeiter & Präsenz','section'),True,True,0)
                team.pack_start(button('Organisation',lambda _:self.show_organizations()),False,False,0)
                if can_manage:team.pack_start(button('+ Mitarbeiter',lambda _:self.employee_form(),'primary'),False,False,0)
                self.content.pack_start(team,False,False,0)
                self.content.pack_start(label('Lokale Mitarbeiteränderungen können über die Administration mit dem Server abgeglichen werden.','muted'),False,False,0)
                self.content.pack_start(button('Eigene Präsenz synchronisieren',lambda _:self.start_personal_presence_sync()),False,False,0)
                personal_auto=Gtk.CheckButton(label='Eigene Präsenz automatisch alle 60 Sekunden abgleichen');personal_auto.set_active(self.presence_auto_enabled);personal_auto.connect('toggled',self.toggle_presence_auto_sync);self.content.pack_start(personal_auto,False,False,0)
                self.presence_auto_notice=label(self.presence_auto_error,'muted');self.content.pack_start(self.presence_auto_notice,False,False,0)
                for employee in data.get('employees',[]):
                    detail=f"ID {employee['id']} · {employee['role']} · {employee['department']} · {employee['presence']} · Systemrolle {employee.get('systemRole') or 'keine'}"
                    states=['office','remote','meeting','away'];current=employee['presence']
                    next_presence=states[(states.index(current)+1)%len(states)] if current in states else 'office'
                    actions=[]
                    if employee['isSelf'] or can_manage:actions.append(('Präsenz weiter',lambda _b,v=employee,state=next_presence:self.run_client(['-action','employee-presence','-id',str(v['id']),'-presence',state],lambda _value,error:self.finished(error))))
                    if can_manage:actions.append(('Bearbeiten',lambda _b,v=employee:self.employee_form(v)))
                    if identity.get('canManageRoles'):actions.append(('Systemrolle',lambda _b,v=employee:self.choose_system_role(v)))
                    if can_manage and not employee['isSelf']:actions.append(('Archivieren',lambda _b,v=employee:self.archive('employee',v)))
                    self.row(employee['name'],detail,extra=actions,wrap_actions=True)
            else:
                team=box(False,8);team.pack_start(label('Zentrales Mitarbeiterverzeichnis','section'),True,True,0)
                team.pack_start(button('Organisation',lambda _:self.show_organizations()),False,False,0)
                if can_manage:team.pack_start(button('+ Mitarbeiter',lambda _:self.employee_form(),'primary'),False,False,0)
                self.content.pack_start(team,False,False,0)
                self.content.pack_start(label('Verzeichniseinträge können einem konfigurierten Serverkonto zugeordnet werden.','muted'),False,False,0)
                for employee in data.get('employees',[]):
                    detail=f"{employee['role']} · {employee['department']} · {employee['presence']} · Serverkonto {employee.get('accountEmployeeId') or 'nicht zugeordnet'}"
                    states=['office','remote','meeting','away'];current=employee['presence'];next_presence=states[(states.index(current)+1)%len(states)] if current in states else 'office'
                    actions=[('Präsenz weiter',lambda _b,v=employee,state=next_presence:self.central_employee_save(v,state)),('Bearbeiten',lambda _b,v=employee:self.employee_form(v)),('Serverkonto',lambda _b,v=employee:self.link_employee_account(v)),('Archivieren',lambda _b,v=employee:self.archive('employee',v))] if can_manage else []
                    self.row(employee['name'],detail,extra=actions,wrap_actions=True)
                if data.get('employees')==[]:self.content.pack_start(label('Keine Mitarbeiter vorhanden.','muted'),False,False,0)
            header=box(False,8)
            header.pack_start(label('Urlaubsanträge & Freigaben','section'),True,True,0)
            if identity.get('canRequestLeave'):header.pack_start(button('+ Urlaubsantrag',lambda _:self.create_leave(),'primary'),False,False,0)
            self.content.pack_start(header,False,False,0)
            if self.local_mode:
                controls=Gtk.FlowBox();controls.set_selection_mode(Gtk.SelectionMode.NONE);controls.set_homogeneous(False);controls.set_column_spacing(8);controls.set_row_spacing(8);controls.set_min_children_per_line(1);controls.set_max_children_per_line(3)
                controls.add(button('Urlaubskonflikt prüfen',lambda _:self.inspect_leave_sync_conflict()))
                if can_manage:controls.add(button('Lokale Urlaubszuordnung prüfen',lambda _:self.inspect_incoming_leave_sync_conflict()))
                controls.add(button('Urlaub synchronisieren',lambda _:self.start_leave_sync()))
                self.content.pack_start(controls,False,False,0)
                leave_auto=Gtk.CheckButton(label='Urlaub automatisch alle 60 Sekunden abgleichen');leave_auto.set_active(self.leave_auto_enabled);leave_auto.connect('toggled',self.toggle_leave_auto_sync);self.content.pack_start(leave_auto,False,False,0)
                self.leave_auto_notice=label(self.leave_auto_error,'muted');self.content.pack_start(self.leave_auto_notice,False,False,0)
            employee_names={}
            for employee in data.get('employees',[]):
                employee_id=employee.get('id') if self.local_mode else employee.get('accountEmployeeId')
                if employee_id:employee_names[employee_id]=employee.get('name','')
            status_labels={'pending':'Offen','approved':'Genehmigt','rejected':'Abgelehnt'}
            for leave in data.get('leave',[]):
                try:
                    start=datetime.fromtimestamp(leave['startsAt']).strftime('%d.%m.%Y %H:%M')
                    end=datetime.fromtimestamp(leave['endsAt']).strftime('%d.%m.%Y %H:%M')
                except (ValueError, OSError, OverflowError, KeyError):
                    start, end = '?', '?'
                person=employee_names.get(leave.get('employeeId')) or ('Mitarbeiter '+str(leave.get('employeeId','?')))
                verified=self.local_mode or leave.get('identityVerified') is True
                origin_unknown=not self.local_mode and leave.get('identityOriginVerified') is False
                applicant_unavailable=not self.local_mode and verified and leave.get('identityOriginVerified') is not None and not leave.get('employeeExternalId')
                decider_unavailable=not self.local_mode and leave.get('identityOriginVerified') is not None and leave.get('status') in ('approved','rejected') and not leave.get('deciderExternalId')
                person_text='Antragsteller-Zuordnung geändert oder nicht verfügbar' if applicant_unavailable else ('Mitarbeiterzuordnung ungeklärt' if not verified else ('Ursprüngliche Person ungeklärt' if origin_unknown else person))
                detail=f"{person_text} · {start} – {end} · {status_labels.get(leave.get('status'),leave.get('status','?'))}"
                if origin_unknown:detail+=' · Ursprüngliche Zuordnung ungeklärt'
                if verified and leave.get('status') in ('approved','rejected'):
                    decider=leave.get('decidedBy')
                    detail+=' · Entscheidend: '+('Zuordnung geändert oder nicht verfügbar' if decider_unavailable else ('Ursprüngliche Person ungeklärt' if origin_unknown else (employee_names.get(decider) or ('Mitarbeiter '+str(decider) if decider else 'unbekannt'))))
                if self.local_mode:
                    sync_labels={'unconfigured':'Urlaubsabgleich nicht eingerichtet','pending':'Übertragung ausstehend','confirmed':'Letzter Stand vom Server bestätigt','unknown':'Serverbestätigung unbekannt'}
                    detail+=' · '+sync_labels.get(leave.get('syncState'),'Serverbestätigung unbekannt')
                actions=[]
                if (not verified or origin_unknown) and not self.local_mode and can_manage and identity.get('canApproveLeave'):
                    actions.append(('Zuordnung klären',lambda _b,v=leave,people=data.get('employees',[]):self.reconcile_legacy_leave(v,people)))
                if verified and not origin_unknown and not applicant_unavailable and leave.get('status')=='pending' and identity.get('canApproveLeave') and leave.get('employeeId')!=actor:
                    actions += [('Genehmigen',lambda _b, v=leave:self.decide_leave(v,'approved')),
                             ('Ablehnen',lambda _b, v=leave:self.decide_leave(v,'rejected'))]
                self.row(leave.get('reason',''),detail,extra=actions)
            if 'leave' in data and not data['leave']:
                self.content.pack_start(label('Keine Urlaubsanträge vorhanden.','muted'),False,False,0)
            return
        titles={1:'Aktive Räume & Ressourcen',2:'Meine Vorgänge',4:'Facility & IT Ticket-Verlauf'}; row=box(False,8)
        row.pack_start(label(titles[self.tab],'section'),True,True,0)
        if self.tab==2:
            if (data.get('identity') or {}).get('canReadTasks'):row.pack_start(button('Freigaben',lambda _:self.show_approvals()),False,False,0)
            if (data.get('identity') or {}).get('canManageProjects'):row.pack_start(button('+ Projekt',lambda _:self.create('project',{'ownerId':(data.get('identity') or {}).get('employeeId') or ''})),False,False,0)
        if self.tab==4: row.pack_start(button('Facility-Assets',lambda _:self.show_assets()),False,False,0)
        if self.tab==1:
            tools=box(False,8);search=Gtk.SearchEntry();search.set_placeholder_text('Räume nach Name, Standort oder Ausstattung suchen');search.set_text(self.room_search_text);self.room_search_widget=search;tools.pack_start(search,True,True,0)
            if self.local_mode and (data.get('identity') or {}).get('canManageRooms'):
                tools.pack_start(button('CSV importieren',lambda _:self.rooms_csv(True)),False,False,0);tools.pack_start(button('CSV exportieren',lambda _:self.rooms_csv(False)),False,False,0)
            self.content.pack_start(tools,False,False,0)
        kind={1:'room',2:'task',4:'ticket'}[self.tab]
        if (data.get('identity') or {}).get({'room':'canManageRooms','task':'canManageTasks','ticket':'canManageTickets'}[kind]):
            actor=(data.get('identity') or {}).get('employeeId') or ''
            row.pack_start(button({'room':'+ Raum anlegen','task':'+ Aufgabe anlegen','ticket':'+ Ticket anlegen'}[kind],lambda _:self.create(kind,{'assigneeId':actor,'canManageProjects':(data.get('identity') or {}).get('canManageProjects')} if kind in ('task','ticket') else None),'primary'),False,False,0)
        self.content.pack_start(row,False,False,0)
        if self.tab==2:
            for p in data.get('projects',[]):
                self.row(p.get('name',''),f"Projekt {p.get('id','')} · {p.get('department','')} · {p.get('status','')}",
                         extra=[('Projektstatus weiter',lambda _b,item=p:self.advance_project_status(item)),
                                ('Bearbeiten', lambda _b, item=p:self.edit('project',item)),
                                ('Archivieren', lambda _b, item=p:self.archive('project',item))] if (data.get('identity') or {}).get('canManageProjects') else [],wrap_actions=True)
        key={1:'rooms',2:'tasks',4:'tickets'}[self.tab]
        room_rows=[]
        for item in data.get(key,[]):
            if key=='rooms':
                identity=data.get('identity') or {};actions=[('Buchungen',lambda _b, v=item:self.show_bookings(v))]
                if identity.get('employeeId') and (self.local_mode or identity.get('canBookRooms')):actions.insert(0,('Buchen',lambda _b,v=item:self.booking_form(v)))
                if identity.get('canManageRooms'):actions.extend([('Sperren',lambda _b,v=item:self.block_room(v)),('Bearbeiten',lambda _b,v=item:self.edit('room',v)),('Archivieren',lambda _b,v=item:self.archive('room',v))])
                card=self.row(item.get('name',''),f"{item.get('location','')} · {item.get('capacity',0)} Plätze · {item.get('equipment','')} · {room_availability_label(item)}",extra=actions,wrap_actions=True)
                room_rows.append((card,' '.join(str(item.get(k,'')) for k in ('name','location','equipment')).casefold()))
            else:
                status=item.get('status',''); next_status={'open':'in_progress','in_progress':'done' if key=='tasks' else 'resolved'}.get(status)
                if key=='tasks' and status in ('done','cancelled'): next_status='open'
                if key=='tickets' and status=='resolved': next_status='closed'
                if key=='tickets' and status=='closed': next_status='open'
                details=f"{item.get('department',item.get('category',''))} · {item.get('priority','')} · {status}"
                extra=[]
                if key=='tasks' and not self.local_mode:
                    if item.get('dueAt'):
                        try:details+=' · fällig '+datetime.fromtimestamp(item['dueAt']).strftime('%d.%m.%Y %H:%M')
                        except (ValueError,OSError,OverflowError):details+=' · Fälligkeit '+str(item['dueAt'])
                    if item.get('recurrenceDays'):details+=f" · alle {item['recurrenceDays']} Tage"
                if self.local_mode and key=='tasks':
                    details+=f" · Projekt-IDs {', '.join(map(str,item.get('projectIds',[]))) or 'keine'} · Mitarbeiter-ID {item.get('assigneeId',0)}"
                    if item.get('dueAt'): details+=' · fällig '+datetime.fromtimestamp(item['dueAt']).strftime('%d.%m.%Y %H:%M')
                    if item.get('recurrenceDays'): details+=f" · alle {item['recurrenceDays']} Tage"
                    extra=[('Details',lambda _b,v=item:self.show_task_details(v))]
                    if (data.get('identity') or {}).get('canManageTasks'):extra.extend([('Bearbeiten',lambda _b,v=item:self.local_task_form(v)),('Projektzuordnungen',lambda _b,v=item:self.show_local_project_assignments(v)),('Archivieren',lambda _b,v=item:self.archive('task',v))])
                if self.local_mode and key=='tickets':
                    details+=f" · Mitarbeiter-ID {item.get('assigneeId',0)}"
                    if (data.get('identity') or {}).get('canReadTickets'):extra=[('Verlauf',lambda _b,v=item:self.show_comments('ticket',v)),('Anhänge',lambda _b,v=item:self.show_attachments('ticket',v))]
                    if (data.get('identity') or {}).get('canManageTickets'):extra.extend([('Bearbeiten',lambda _b,v=item:self.local_ticket_form(v)),('Archivieren',lambda _b,v=item:self.archive('ticket',v))])
                if not self.local_mode:
                    extra.extend([('Verlauf',lambda _b,v=item,k=kind:self.show_comments(k,v)),('Anhänge',lambda _b,v=item,k=kind:self.show_attachments(k,v))])
                    if (data.get('identity') or {}).get('canManageTasks' if kind=='task' else 'canManageTickets'):extra.extend([('Bearbeiten',lambda _b,v=item,k=kind,pm=bool((data.get('identity') or {}).get('canManageProjects')):self.central_work_form(k,v,pm)),('Archivieren',lambda _b,v=item,k=kind:self.archive(k,v))])
                if not self.local_mode and key=='tasks':
                    extra.insert(0,('Details',lambda _b,v=item:self.show_task_details(v)))
                    if (data.get('identity') or {}).get('canManageTasks') and (data.get('identity') or {}).get('canManageProjects'):extra.append(('Projektzuordnungen',lambda _b,v=item:self.show_central_project_assignments(v)))
                    if (data.get('identity') or {}).get('canManageTasks'):extra.append(('Freigabe anfordern',lambda _b,v=item:self.request_approval(v)))
                can_manage=(data.get('identity') or {}).get('canManageTasks' if kind=='task' else 'canManageTickets')
                self.row(item.get('title',''),details,(lambda _b,k=kind,v=item,s=next_status:self.change_status(k,v['id'],s)) if next_status and can_manage else None,extra,wrap_actions=True)
        if key=='rooms':
            def filter_rooms(entry):
                self.room_search_text=entry.get_text();query=self.room_search_text.strip().casefold()
                for card,text in room_rows:
                    visible=query in text;card.set_no_show_all(not visible)
                    if visible:card.show_all()
                    else:card.hide()
            search.connect('changed',filter_rooms);filter_rooms(search)
        if key in data and not data[key]: self.content.pack_start(label('Noch keine Einträge vorhanden.','muted'),False,False,0)

    def load_dashboard_schedule(self,container,rooms,generation):
        mode=self.local_mode;session=self.local;connection=(self.url,self.token,self.ca)
        container.pack_start(label('Heutiger Raum- & Terminplan','section'),False,False,0)
        notice=label('Termine werden geladen …','muted');container.pack_start(notice,False,False,0)
        if rooms is None:notice.set_text('Raumtermine konnten nicht geladen werden.');return
        today=datetime.now().date();start=datetime.combine(today,datetime.min.time()).timestamp();end=datetime.combine(today+timedelta(days=1),datetime.min.time()).timestamp()
        entries=[];errors=[]
        def current():return generation==self.generation and self.tab==0 and self.local_mode==mode and ((mode and self.local is session) or (not mode and connection==(self.url,self.token,self.ca)))
        def fetch(index):
            if not current():return
            if index>=len(rooms):
                entries.sort(key=lambda v:(v[0],v[1].casefold()))
                notice.set_text('Einige Raumtermine konnten nicht geladen werden.' if errors else ('' if entries else 'Heute keine Buchungen oder Sperren.'))
                for begins,name,record in entries:
                    try:period=datetime.fromtimestamp(begins).strftime('%d.%m. %H:%M')+' – '+datetime.fromtimestamp(record['endsAt']).strftime('%d.%m. %H:%M')
                    except (ValueError,OverflowError,OSError):continue
                    status='Sperre' if record.get('status')=='blocked' else 'Buchung'
                    container.pack_start(label(period+' · '+name+' · '+status,'muted'),False,False,0)
                container.show_all();return
            room=rooms[index]
            def loaded(records,error):
                if not current():return
                if error or not isinstance(records,list):errors.append(room.get('name',''))
                else:
                    for record in records:
                        begin=record.get('startsAt');finish=record.get('endsAt')
                        if record.get('status','confirmed') in ('confirmed','blocked') and isinstance(begin,(int,float)) and isinstance(finish,(int,float)) and begin<end and finish>start:
                            entries.append((begin,room.get('name',''),record))
                fetch(index+1)
            args=['-action','bookings','-room-id',str(room['id'])] if mode else ['-action','central-bookings','-room-key',str(room['id'])]
            self.run_client(args,loaded)
        fetch(0)

    def row(self,title,detail,action=None,extra=None,wrap_actions=False):
        card=box(False,10,'card'); column=box(True,5); column.pack_start(label(title,'section'),False,False,0); column.pack_start(label(detail,'muted'),False,False,0); card.pack_start(column,True,True,0)
        if wrap_actions and (extra or action):
            actions=([('Status weiter',action)] if action else [])+list(extra or [])
            controls=box(True,4)
            for index in range(0,len(actions),3):
                line=box(False,4)
                for text,callback in actions[index:index+3]:line.pack_start(button(text,callback),False,False,0)
                controls.pack_start(line,False,False,0)
            card.pack_start(controls,False,False,0)
        else:
            if action:card.pack_start(button('Status weiter',action),False,False,0)
            for text, callback in (extra or []): card.pack_start(button(text,callback),False,False,0)
        self.content.pack_start(card,False,False,0);return card
    @staticmethod
    def parse_local_time(value):
        local=datetime.strptime(value,'%Y-%m-%d %H:%M')
        timestamp=int(local.timestamp())
        if timestamp <= 0 or datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M') != value:
            raise ValueError('Ungültige Ortszeit')
        return timestamp

    def rooms_csv(self,importing):
        if not self.local_mode:return
        action=Gtk.FileChooserAction.OPEN if importing else Gtk.FileChooserAction.SAVE
        dialog=Gtk.FileChooserDialog(title='Räume importieren' if importing else 'Räume exportieren',transient_for=self,action=action)
        dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Importieren' if importing else 'Exportieren',Gtk.ResponseType.OK)
        if not importing:dialog.set_current_name('raeume.csv')
        response=dialog.run();path=dialog.get_filename();dialog.destroy()
        if response!=Gtk.ResponseType.OK or not path:return
        if importing:
            confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Räume importieren?')
            confirm.format_secondary_text('Räume mit gleichem Namen werden aktualisiert; der Aktivstatus wird übernommen.')
            confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Importieren',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
            if response!=Gtk.ResponseType.OK:return
        generation=self.generation
        def done(result,error):
            if generation!=self.generation or not self.local_mode:return
            if error:self.message(error);return
            info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Räume importiert' if importing else 'Räume exportiert')
            info.format_secondary_text((str(result['count'])+' Räume importiert.') if importing else path);info.run();info.destroy();self.refresh()
        self.run_client(['-action','rooms-import' if importing else 'rooms-export','-path',path],done)

    def show_bookings(self,room):
        mode=self.local_mode
        self.generation+=1; generation=self.generation; self.booking_view_generation=generation;self.booking_view_room=room;self.clear()
        self.content.pack_start(label('Buchungen werden geladen …','muted'),False,False,0); self.render()
        identity={};identity_error=None
        def done(records,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear(); header=box(False,8)
            header.pack_start(label('Buchungen · '+room.get('name',''),'section'),True,True,0)
            header.pack_start(button('← Räume',lambda _:self.select(1)),False,False,0)
            if identity.get('canManageRooms'):header.pack_start(button('+ Sperrzeit',lambda _:self.block_room(room)),False,False,0)
            if identity.get('employeeId') and (mode or identity.get('canBookRooms')):header.pack_start(button('+ Buchung',lambda _:self.booking_form(room),'primary'),False,False,0)
            self.content.pack_start(header,False,False,0)
            if mode:self.content.pack_start(action_flow([button('Buchungskonflikt prüfen',lambda _:self.inspect_booking_sync_conflict(room)),button('Buchungen synchronisieren',lambda _:self.start_booking_sync(room))]),False,False,0)
            self.content.pack_start(label('Lokale und zentrale Buchungen werden beim Abgleich zusammengeführt; offene Konflikte halten den Abgleich an.' if mode else 'Zentrale Buchungen. Alte Buchungen ohne Raumkennung können zusätzlich Konflikte verursachen.','muted'),False,False,0)
            if mode:
                auto=Gtk.CheckButton(label='Buchungen automatisch alle 60 Sekunden abgleichen');auto.set_active(self.booking_auto_enabled);auto.connect('toggled',self.toggle_booking_auto_sync);self.content.pack_start(auto,False,False,0)
                if self.booking_auto_error:self.content.pack_start(label(self.booking_auto_error,'muted'),False,False,0)
            if error: self.content.pack_start(label(error,'muted'),False,False,0)
            if identity_error:self.content.pack_start(label('Sitzungsrechte konnten nicht geladen werden: '+str(identity_error),'muted'),False,False,0)
            for booking in (records or []):
                try:
                    start=datetime.fromtimestamp(booking['startsAt']).strftime('%d.%m.%Y %H:%M')
                    end=datetime.fromtimestamp(booking['endsAt']).strftime('%d.%m.%Y %H:%M')
                except (OSError,ValueError,OverflowError,KeyError): start,end='?','?'
                status={'confirmed':'Gebucht','cancelled':'Storniert','blocked':'Gesperrt'}.get(booking.get('status','confirmed'),booking.get('status','confirmed'))
                detail=f"{start} – {end} · {booking.get('attendees') or '?'} Teilnehmer · Mitarbeiter {booking.get('userId') or '?'}"
                if booking.get('reason'):detail+=' · '+booking['reason']
                actions=[]
                owner=booking.get('userId') or 0
                editable=(identity.get('canManageRooms') or (owner>0 and owner==identity.get('employeeId'))) and (not mode or owner>0)
                if booking.get('status','confirmed')=='confirmed' and editable:
                    actions=[('Umbuchen',lambda _b,v=booking:self.booking_form(room,v)),
                             ('Stornieren',lambda _b,v=booking:self.cancel_booking(room,v))]
                if mode and identity.get('canManageRooms') and not booking.get('userId') and booking.get('remoteClient') and booking.get('status') in ('confirmed','cancelled'):
                    actions.append(('Person zuordnen',lambda _b,v=booking:self.reconcile_legacy_booking(room,v)))
                self.row(status,detail,extra=actions)
            if records==[]: self.content.pack_start(label('Noch keine Buchungen vorhanden.','muted'),False,False,0)
            self.render()
        def loaded_identity(value,error):
            nonlocal identity_error
            if generation!=self.generation or self.local_mode!=mode:return
            if not error and isinstance(value,dict):identity.update(value)
            else:identity_error=error or 'Ungültige Antwort'
            self.run_client(['-action','bookings','-room-id',str(room['id'])] if mode else ['-action','central-bookings','-room-key',str(room['id'])],done)
        self.run_client(['-action','identity'],loaded_identity)

    def reconcile_legacy_booking(self,room,booking):
        if not self.local_mode or not self.local or self.room_sync_running:return
        values=self.form('Buchungszuordnung prüfen',[('Server-URL',self.url,False),('Administrator-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;generation=self.generation;self.room_sync_running=True;self.state.set_text('Buchungszuordnung wird geprüft …')
        current=lambda:self.local_mode and self.local is session and not self.window_closed
        def work():
            try:
                from booking_sync import inspect_legacy_booking
                reviewed=inspect_legacy_booking(session,self.local_lock,current,CLIENT,url,token,ca,booking['id']);error=None
            except (LocalStoreError,OSError,ValueError,KeyError) as exc:reviewed=None;error=str(exc)
            def done():
                if not current() or generation!=self.generation:self.room_sync_running=False;return False
                if error:self.room_sync_running=False;self.message(error);return False
                dialog=Gtk.Dialog(title='Buchungszuordnung klären',transient_for=self,modal=True);dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Weiter',Gtk.ResponseType.OK)
                content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
                try:
                    start_text=datetime.fromtimestamp(booking['startsAt']).strftime('%d.%m.%Y %H:%M')
                    end_text=datetime.fromtimestamp(booking['endsAt']).strftime('%d.%m.%Y %H:%M')
                except (OSError,ValueError,OverflowError):start_text,end_text='?','?'
                content.pack_start(label('Historische Buchung: '+start_text+' – '+end_text+'\nDer bisherige Eigentümer ist nicht bekannt.'),False,False,0)
                content.pack_start(label('Buchende Person auswählen:'),False,False,0);people=Gtk.ComboBoxText()
                for person in reviewed['employees']:people.append(person['id'],person['name']+' · Konto '+str(person['accountEmployeeId']))
                content.pack_start(people,False,False,0);content.pack_start(label('Teilnehmerzahl:'),False,False,0)
                count=Gtk.SpinButton.new_with_range(1,max(1,int(room.get('capacity',1))),1);count.set_value(max(1,int(booking.get('attendees') or 1)));content.pack_start(count,False,False,0)
                dialog.show_all();response=dialog.run();employee=people.get_active_id();attendees=count.get_value_as_int();dialog.destroy()
                if response!=Gtk.ResponseType.OK:self.room_sync_running=False;return False
                if not employee:self.room_sync_running=False;self.message('Bitte eine Person auswählen.');return False
                selected=next(p for p in reviewed['employees'] if p['id']==employee)
                confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Buchungszuordnung bestätigen?')
                confirm.format_secondary_text('Person: '+selected['name']+' · Konto '+str(selected['accountEmployeeId'])+'\nTeilnehmer: '+str(attendees)+'\nDie Zuordnung wird auf dem Server protokolliert.')
                confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Zuordnen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
                if response!=Gtk.ResponseType.OK or not current() or generation!=self.generation:self.room_sync_running=False;return False
                def commit():
                    try:
                        from booking_sync import reconcile_legacy_booking, sync_bookings
                        reconcile_legacy_booking(session,self.local_lock,current,CLIENT,url,token,ca,reviewed,employee,attendees)
                        try:sync_bookings(session,self.local_lock,current,CLIENT,url,token,ca);problem=None
                        except (LocalStoreError,OSError,ValueError) as exc:problem='Die Zuordnung wurde auf dem Server gespeichert. Der lokale Abgleich ist noch offen: '+str(exc)
                    except (LocalStoreError,OSError,ValueError,KeyError) as exc:problem=str(exc)
                    def finished():
                        self.room_sync_running=False
                        if not current():return False
                        if problem:self.message(problem)
                        self.show_bookings(room);return False
                    GLib.idle_add(finished)
                threading.Thread(target=commit,daemon=True).start();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def stop_booking_auto_sync(self):
        self.booking_auto_enabled=False;self.booking_auto_session=None;self.booking_auto_credentials=None
        if self.booking_auto_source:
            GLib.source_remove(self.booking_auto_source);self.booking_auto_source=0

    def toggle_booking_auto_sync(self,widget):
        if not widget.get_active():self.stop_booking_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst den Buchungsabgleich manuell einrichten.');return
        session=self.local
        try:
            from leave_sync import transaction
            with transaction(session,self.local_lock,lambda:self.local is session and self.local_mode) as (db,_actor):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_booking_sync_config'").fetchone() or db.execute('SELECT server_url FROM linux_booking_sync_config').fetchone()!=(self.url.strip().rstrip('/'),):
                    raise LocalStoreError('Bitte den Buchungsabgleich für diesen Server zuerst manuell einrichten.')
        except (LocalStoreError,sqlite3.Error) as exc:
            widget.set_active(False);self.message(str(exc));return
        self.booking_auto_enabled=True;self.booking_auto_session=session;self.booking_auto_credentials=(self.url,self.token,self.ca);self.booking_auto_error=''
        self.booking_auto_source=GLib.timeout_add_seconds(self.booking_auto_interval,self.booking_auto_tick);self.booking_auto_tick()

    def booking_auto_tick(self):
        if self.window_closed or not self.booking_auto_enabled or not self.local_mode or self.local is not self.booking_auto_session:
            self.booking_auto_source=0;return False
        if self.room_sync_running:return True
        session=self.local;url,token,ca=self.booking_auto_credentials;generation=self.generation;self.room_sync_running=True;self.state.set_text('Automatischer Buchungsabgleich …')
        def work():
            try:
                result=sync_bookings(session,self.local_lock,lambda:self.local is session and self.local_mode and self.booking_auto_enabled and self.booking_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,BookingPendingError)
                if pending:result={'received':exc.received}
            def done():
                self.room_sync_running=False
                if self.window_closed or not self.booking_auto_enabled or self.local is not session or self.booking_auto_session is not session:return False
                old_error=self.booking_auto_error
                if error:
                    if pending:self.booking_auto_error='Buchungsabgleich wartet auf den Raumabgleich oder eine ältere Änderung der zugehörigen Person.';self.state.set_text('Buchungsabgleich wartet')
                    elif retryable:self.booking_auto_error='Server vorübergehend nicht erreichbar; erneuter Versuch folgt.';self.state.set_text('Buchungsabgleich wartet auf Server')
                    else:self.stop_booking_auto_sync();self.booking_auto_error='Automatischer Buchungsabgleich angehalten: '+error;self.state.set_text('Buchungsabgleich angehalten · bitte prüfen')
                else:
                    self.booking_auto_error=(str(result['unmapped'])+' Buchungen nicht vollständig zugeordnet.' if result['unmapped'] else '');self.state.set_text('Buchungsabgleich automatisch aktiv')
                if self.generation==generation and self.booking_view_generation==generation and (old_error!=self.booking_auto_error or (result and result['received'])):self.show_bookings(self.booking_view_room)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def inspect_booking_sync_conflict(self,room):
        if not self.local_mode:return
        if self.room_sync_running:self.message('Ein Raum- oder Buchungsabgleich läuft bereits.');return
        values=self.form('Buchungskonflikt prüfen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;generation=self.generation;self.room_sync_running=True;self.state.set_text('Buchungskonflikt wird geprüft …')
        def work():
            try:result=inspect_booking_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.room_sync_running=False;return False
                if error:self.room_sync_running=False;self.state.set_text('Buchungskonflikt nicht geprüft');self.message(error);return False
                def date(value):
                    try:return datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M')
                    except (ValueError,OSError,OverflowError):return str(value)
                def describe(item):return date(item['startsAt'])+' – '+date(item['endsAt'])+' · '+str(item.get('attendees') or '?')+' Teilnehmer · '+str(item.get('status') or 'confirmed')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Buchungskonflikt entscheiden')
                is_create=result['preview']['action']=='create'
                explanation='Beim Übernehmen wird die eigene lokale Buchung storniert.' if is_create else 'Beim Übernehmen werden die ausstehenden eigenen Änderungen verworfen und der Serverstand lokal übernommen.'
                original=result['preview']['creation']['booking']
                original_text='\n\nUrsprünglich ausstehender Termin:\n'+describe(original) if is_create and any(original[k]!=result['preview']['wire'][k] for k in ('startsAt','endsAt','attendees','status')) else ''
                dialog.format_secondary_text('Lokale Buchung:\n'+describe(result['preview']['local'])+original_text+'\n\nServertermine:\n'+'\n'.join(describe(v) for v in result['conflicts'])+'\n\n'+explanation+' Die Entscheidung und Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Servertermine übernehmen',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response!=Gtk.ResponseType.OK:self.room_sync_running=False;self.state.set_text('Buchungskonflikt unverändert');return False
                confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Lokale Buchung stornieren?' if is_create else 'Serverbuchung lokal übernehmen?')
                confirm.format_secondary_text(('Die eigene lokale Buchung wird storniert. ' if is_create else 'Die ausstehenden eigenen Änderungen werden verworfen. ')+'Der geprüfte Serverstand wird übernommen; die bisherigen Änderungen bleiben im lokalen Protokoll erhalten.')
                confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Stornieren und übernehmen' if is_create else 'Serverstand übernehmen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
                if response!=Gtk.ResponseType.OK:self.room_sync_running=False;self.state.set_text('Buchungskonflikt unverändert');return False
                def apply():
                    try:accept_server_booking_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.room_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Buchungskonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.booking_auto_error='';self.state.set_text('Servertermine übernommen')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Servertermine übernommen');info.run();info.destroy()
                        if self.generation==generation:self.show_bookings(room)
                        return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_booking_sync(self,room):
        if not self.local_mode:return
        if self.room_sync_running:self.message('Ein Raum- oder Buchungsabgleich läuft bereits.');return
        values=self.form('Buchungen zum Server senden',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und persönliches Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Buchungen senden?')
        confirm.format_secondary_text('Die angemeldete Person muss ihrem Serverkonto zugeordnet sein. Eigene Buchungen, selbst angelegte Sperrzeiten und eigene Verwaltungsänderungen werden übertragen. Räume und Mitarbeiter müssen zuvor abgeglichen sein. Zentrale Buchungen und Sperrzeiten werden lokal übernommen, wenn sie eindeutig zugeordnet werden können. Bei Überschneidungen bleibt der Abgleich stehen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Senden',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;generation=self.generation;self.room_sync_running=True;self.state.set_text('Buchungen werden gesendet …')
        def work():
            try:result=sync_bookings(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.room_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Buchungsübertragung angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.state.set_text('Buchungsübertragung abgeschlossen')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Buchungsübertragung abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Buchungen übernommen. Nicht eindeutig zugeordnet: '+str(result['unmapped'])+'.');dialog.run();dialog.destroy()
                if self.generation==generation:self.show_bookings(room)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def booking_form(self,room,booking=None):
        if booking:
            start=datetime.fromtimestamp(booking['startsAt']).strftime('%Y-%m-%d %H:%M')
            end=datetime.fromtimestamp(booking['endsAt']).strftime('%Y-%m-%d %H:%M')
        else:
            rounded=((int(time.time())+299)//300)*300
            start=datetime.fromtimestamp(rounded).strftime('%Y-%m-%d %H:%M')
            end=datetime.fromtimestamp(rounded+3600).strftime('%Y-%m-%d %H:%M')
        fields=[('Beginn (JJJJ-MM-TT SS:MM, Ortszeit)',start,False),
                ('Ende (JJJJ-MM-TT SS:MM, Ortszeit)',end,False),
                ('Teilnehmer',(booking.get('attendees') or 1) if booking else '1',False)]
        if not booking: fields.extend([('Anzahl Termine (1–104)','1',False),('Abstand in Tagen (1–366)','7',False)])
        values=self.form('Buchung ändern' if booking else 'Raum buchen',fields)
        if values is None:return
        try:
            start=self.parse_local_time(values[0]); end=self.parse_local_time(values[1]); attendees=int(values[2])
            occurrences=int(values[3]) if not booking else 1
            interval=int(values[4]) if not booking else 7
            if end<=start or attendees<=0 or not 1<=occurrences<=104 or not 1<=interval<=366: raise ValueError()
        except (ValueError,OverflowError,OSError):
            self.message('Bitte gültige Ortszeiten, Teilnehmerzahl und Serieneinstellungen eingeben.');return
        if self.local_mode:
            args=['-action','reschedule' if booking else 'book','-room-id',str(room['id']),'-start',str(start),'-end',str(end),'-attendees',str(attendees)]
            if booking:args+=['-id',str(booking['id'])]
        else:
            args=['-action','central-reschedule' if booking else 'central-book','-room-key',str(room['id']),'-starts-at',str(start),'-ends-at',str(end),'-attendees',str(attendees)]
            if booking:args+=['-booking-id',str(booking['bookingId']),'-booking-client',booking['clientId']]
        if not booking:args+=['-occurrences',str(occurrences),'-interval',str(interval)]
        self.booking_action(room,args)

    def booking_action(self,room,args):
        generation=self.generation;mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error: self.message(error)
            else: self.show_bookings(room)
        self.run_client(args,done)

    def cancel_booking(self,room,booking):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Buchung stornieren?')
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Stornieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:
            self.booking_action(room,['-action','cancel-booking','-room-id',str(room['id']),'-id',str(booking['id'])] if self.local_mode else ['-action','central-cancel-booking','-room-key',str(room['id']),'-booking-id',str(booking['bookingId']),'-booking-client',booking['clientId']])

    def block_room(self,room):
        values=self.form('Sperrzeit anlegen',[('Beginn (JJJJ-MM-TT SS:MM, Ortszeit)','',False),
            ('Ende (JJJJ-MM-TT SS:MM, Ortszeit)','',False),('Grund','',False)])
        if values is None:return
        try:
            start=self.parse_local_time(values[0]);end=self.parse_local_time(values[1])
            if end<=start or not values[2]:raise ValueError()
        except (ValueError,OverflowError,OSError):
            self.message('Bitte gültige Ortszeiten und einen Grund eingeben.');return
        self.booking_action(room,['-action','block-room','-room-id',str(room['id']),'-start',str(start),'-end',str(end),'-reason',values[2]] if self.local_mode else ['-action','central-block-room','-room-key',str(room['id']),'-starts-at',str(start),'-ends-at',str(end),'-reason',values[2]])

    def show_task_details(self,task):
        local_mode=self.local_mode
        self.generation+=1; generation=self.generation; self.clear()
        self.content.pack_start(label('Aufgabendetails werden geladen …','muted'),False,False,0); self.render()
        data={}; errors=[]
        requests=[('identity',['-action','identity']),('dependencies',['-action','dependencies','-task-id',str(task['id'])]),('subtasks',['-action','subtasks','-task-id',str(task['id'])]),('tasks',['-action','tasks'])]
        def load(index):
            if generation!=self.generation or self.local_mode!=local_mode:return
            if index<len(requests):
                key,args=requests[index]
                def done(value,error):
                    if error:errors.append(error)
                    else:data[key]=value or []
                    load(index+1)
                self.run_client(args,done);return
            self.clear(); header=box(False,8);can_manage=bool((data.get('identity') or {}).get('canManageTasks'))
            header.pack_start(label('Aufgabe · '+task.get('title',''),'section'),True,True,0)
            header.pack_start(button('← Aufgaben',lambda _:self.select(2)),False,False,0)
            self.content.pack_start(header,False,False,0)
            actions=[]
            if local_mode:actions.append(button('Abgleiche öffnen',lambda _:self.select(5)))
            if can_manage:actions.append(button('Freigabe anfordern',lambda _:self.request_approval(task)))
            if can_manage or not local_mode or (data.get('identity') or {}).get('canReadTasks'):
                actions.append(button('Anhänge',lambda _:self.show_attachments('task',task)))
                actions.append(button('Verlauf',lambda _:self.show_comments('task',task)))
            if actions:self.content.pack_start(action_flow(actions),False,False,0)
            self.content.pack_start(label('Lokale Aufgaben, Unteraufgaben und Voraussetzungen können über Administration → Lokale Aufgaben synchronisieren abgeglichen werden.' if local_mode else 'Zentrale Voraussetzungen werden auf dem Server geprüft.','muted'),False,False,0)
            for error in errors:self.content.pack_start(label(error,'muted'),False,False,0)
            row=box(False,8);row.pack_start(label('Voraussetzungen','section'),True,True,0)
            if can_manage:row.pack_start(button('+ Abhängigkeit',lambda _:self.add_dependency(task,data.get('tasks',[]),data.get('dependencies',[]))),False,False,0)
            self.content.pack_start(row,False,False,0)
            for dep in data.get('dependencies',[]):
                self.row(dep.get('title') or ('Aufgabe '+str(dep['id'])),f"Aufgabe #{dep['id']}",
                         extra=[('Entfernen',lambda _b,v=dep:self.task_detail_action(task,['-action','dependency-remove','-task-id',str(task['id']),'-dependency-id',str(v['id'])]))] if can_manage else [])
            if 'dependencies' in data and not data['dependencies']:self.content.pack_start(label('Keine Voraussetzungen.','muted'),False,False,0)
            row=box(False,8);row.pack_start(label('Unteraufgaben','section'),True,True,0)
            if can_manage:row.pack_start(button('+ Unteraufgabe',lambda _:self.subtask_form(task),'primary'),False,False,0)
            self.content.pack_start(row,False,False,0)
            for sub in data.get('subtasks',[]):
                state={'open':'Offen','in_progress':'In Bearbeitung','done':'Erledigt','cancelled':'Storniert'}.get(sub['status'],sub['status'])
                details=f"{state} ({sub['status']}) · "+('Nicht zugewiesen' if not sub['assigneeId'] else ('Mitarbeiter-ID ' if local_mode else 'Serverkonto-ID ')+str(sub['assigneeId']))
                if sub['dueAt']:details+=' · fällig '+datetime.fromtimestamp(sub['dueAt']).strftime('%d.%m.%Y %H:%M')
                self.row(sub['title'],details,extra=[('Bearbeiten',lambda _b,v=sub:self.subtask_form(task,v)),
                         ('Archivieren',lambda _b,v=sub:self.archive_subtask(task,v))] if can_manage else [])
            if 'subtasks' in data and not data['subtasks']:self.content.pack_start(label('Keine Unteraufgaben.','muted'),False,False,0)
            self.render()
        load(0)

    def task_detail_action(self,task,args):
        generation=self.generation;local_mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=local_mode:return
            if error:
                if '-payload' in args and ('409' in str(error) or 'Unteraufgabe wurde geändert' in str(error)):self.message('Die Unteraufgabe wurde zwischenzeitlich geändert oder die Aktion konnte nicht ausgeführt werden. Bitte erneut öffnen und prüfen.\n'+str(error));self.show_task_details(task)
                else:self.message(error)
            else:self.show_task_details(task)
        self.run_client(args,done)

    def add_dependency(self,task,tasks,dependencies):
        existing={d['id'] for d in dependencies}
        candidates=[t for t in tasks if t['id']!=task['id'] and t['id'] not in existing]
        if not candidates:self.message('Keine weitere aktive Aufgabe verfügbar.');return
        dialog=Gtk.Dialog(title='Voraussetzung auswählen',transient_for=self,modal=True)
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Hinzufügen',Gtk.ResponseType.OK)
        content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
        content.pack_start(label('Diese Aufgabe kann erst nach Abschluss der ausgewählten Aufgabe erledigt werden.'),False,False,0)
        choice=Gtk.ComboBoxText()
        for candidate in candidates:choice.append(str(candidate['id']),candidate['title']+' · '+candidate['status'])
        choice.set_active(0);content.pack_start(choice,False,False,0);dialog.set_default_size(450,-1);dialog.show_all()
        response=dialog.run(); selected=choice.get_active_id();dialog.destroy()
        if response==Gtk.ResponseType.OK and selected:
            self.task_detail_action(task,['-action','dependency-add','-task-id',str(task['id']),'-dependency-id',selected])

    def subtask_form(self,task,sub=None,compare=None):
        if sub and not self.local_mode and compare is None:
            generation=self.generation;connection=(self.url,self.token,self.ca)
            def capabilities(value,error):
                if self.local_mode or self.generation!=generation or (self.url,self.token,self.ca)!=connection:return
                if error and '404 Not Found' not in str(error):self.message('Serverfunktionen konnten nicht geladen werden: '+str(error));return
                supported=isinstance(value,dict) and value.get('centralSubtaskCompare')==1
                if not supported:self.state.set_text('Unteraufgabenvergleich mit diesem Server nicht verfügbar')
                self.subtask_form(task,sub,supported)
            self.run_client(['-action','capabilities'],capabilities);return
        sub=sub or {};due=datetime.fromtimestamp(sub['dueAt']).strftime('%Y-%m-%d %H:%M') if sub.get('dueAt') else ''
        fields=[('Titel',sub.get('title',''),False),('Status (open, in_progress, done, cancelled)',sub.get('status','open'),False),
                (('Mitarbeiter-ID' if self.local_mode else 'Serverkonto-ID der zuständigen Person')+' (0 = nicht zugewiesen)',sub.get('assigneeId',0),False),('Fällig (JJJJ-MM-TT SS:MM, optional)',due,False)]
        values=self.form('Unteraufgabe bearbeiten' if sub else 'Unteraufgabe anlegen',fields)
        if values is None:return
        try:
            assignee=int(values[2]);when=self.parse_local_time(values[3]) if values[3] else 0
            if assignee<0 or values[1] not in ('open','in_progress','done','cancelled'):raise ValueError()
        except (ValueError,OverflowError,OSError):
            self.message('Bitte gültigen Status, Mitarbeiter-ID und Fälligkeit eingeben.');return
        args=['-action','subtask-save','-task-id',str(task['id']),'-id',str(sub.get('id',0)), '-title',values[0],'-status',values[1],'-assignee-id',str(assignee),'-due',str(when)]
        if sub and (self.local_mode or compare):args+=['-payload',json.dumps(sub,ensure_ascii=False)]
        self.task_detail_action(task,args)

    def archive_subtask(self,task,sub,compare=None):
        if not self.local_mode and compare is None:
            generation=self.generation;connection=(self.url,self.token,self.ca)
            def capabilities(value,error):
                if self.local_mode or self.generation!=generation or (self.url,self.token,self.ca)!=connection:return
                if error and '404 Not Found' not in str(error):self.message('Serverfunktionen konnten nicht geladen werden: '+str(error));return
                supported=isinstance(value,dict) and value.get('centralSubtaskArchiveCompare')==1
                if not supported:self.state.set_text('Archivierungsvergleich mit diesem Server nicht verfügbar')
                self.archive_subtask(task,sub,supported)
            self.run_client(['-action','capabilities'],capabilities);return
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Unteraufgabe archivieren?')
        dialog.format_secondary_text(sub.get('title',''));dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:
            args=['-action','subtask-archive','-task-id',str(task['id']),'-id',str(sub['id'])]
            if self.local_mode or compare:args+=['-payload',json.dumps(sub,ensure_ascii=False)]
            self.task_detail_action(task,args)

    def local_facility_action(self,args,reload):
        generation=self.generation; mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error:self.message(error)
            else:reload()
        self.run_client(args,done)

    def show_assets(self):
        mode=self.local_mode
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Assets werden geladen …','muted'),False,False,0);self.render()
        def done(assets,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8);header.pack_start(label('Facility-Assets','section'),True,True,0)
            header.pack_start(button('← Helpdesk',lambda _:self.select(4)),False,False,0)
            if mode:header.pack_start(button('Abgleiche öffnen',lambda _:self.select(5)),False,False,0)
            header.pack_start(button('+ Asset',lambda _:self.asset_form(),'primary'),False,False,0);self.content.pack_start(header,False,False,0)
            self.content.pack_start(label('Lokale Anlagen können über Administration → Facility-Anlagen synchronisieren abgeglichen werden.' if mode else 'Zentrale Facility-Assets','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for asset in assets or []:
                detail=f"{asset['category']} · {asset['location']} · {asset['status']}"
                actions=[('Bearbeiten',lambda _b,v=asset:self.asset_form(v)),('Archivieren',lambda _b,v=asset:self.archive_facility('asset',v))]
                actions.insert(0,('Wartung',lambda _b,v=asset:self.show_maintenance(v)))
                self.row(asset['name'],detail,extra=actions)
            if assets==[]:self.content.pack_start(label('Keine Assets vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','assets' if mode else 'facility-assets'],done)

    def asset_form(self,asset=None):
        asset=asset or {};fields=[('Name',asset.get('name',''),False),('Kategorie',asset.get('category',''),False),
            ('Standort',asset.get('location',''),False),('Status (operational, maintenance, out_of_service)',asset.get('status','operational'),False)]
        values=self.form('Asset bearbeiten' if asset else 'Asset anlegen',fields)
        if values is None:return
        if self.local_mode:
            args=['-action','asset-save','-id',str(asset.get('id',0)),'-name',values[0],'-category',values[1],'-location',values[2],'-status',values[3]]
        else:
            args=['-action','update-facility-asset' if asset else 'create-facility-asset','-name',values[0],'-asset-category',values[1],'-location',values[2],'-asset-status',values[3]]
            if asset:args+=['-id',str(asset['id'])]
        self.local_facility_action(args,self.show_assets)

    def show_maintenance(self,asset):
        mode=self.local_mode
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Wartung wird geladen …','muted'),False,False,0);self.render()
        def done(orders,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8);header.pack_start(label('Wartung · '+asset.get('name',''),'section'),True,True,0)
            header.pack_start(button('← Assets',lambda _:self.show_assets()),False,False,0)
            if mode:header.pack_start(button('Abgleiche öffnen',lambda _:self.select(5)),False,False,0)
            header.pack_start(button('+ Wartungsauftrag',lambda _:self.maintenance_form(asset),'primary'),False,False,0);self.content.pack_start(header,False,False,0)
            self.content.pack_start(label('Lokale Wartungsaufträge können über Administration → Wartungsaufträge synchronisieren abgeglichen werden.' if mode else 'Zentrale Wartungsaufträge','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for order in orders or []:
                detail=order['status']
                if order['dueAt']:detail+=' · fällig '+datetime.fromtimestamp(order['dueAt']).strftime('%d.%m.%Y %H:%M')
                if order['recurrenceDays']:detail+=f" · alle {order['recurrenceDays']} Tage"
                actions=[('Bearbeiten',lambda _b,v=order:self.maintenance_form(asset,v)),
                         ('Archivieren',lambda _b,v=order:self.archive_facility('maintenance',v,asset))]
                next_status={'open':'in_progress','in_progress':'completed','completed':'open','cancelled':'open'}.get(order['status'])
                if next_status:actions.insert(0,('Status weiter',lambda _b,v=order,state=next_status:self.local_facility_action(['-action','maintenance-status' if mode else 'central-maintenance-status','-asset-id',str(asset['id']),'-id',str(v['id']),'-status',state],lambda:self.show_maintenance(asset))))
                self.row(order['title'],detail,extra=actions)
            if orders==[]:self.content.pack_start(label('Keine Wartungsaufträge vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','maintenance' if mode else 'central-maintenance','-asset-id',str(asset['id'])],done)

    def maintenance_form(self,asset,order=None):
        order=order or {};due=datetime.fromtimestamp(order['dueAt']).strftime('%Y-%m-%d %H:%M') if order.get('dueAt') else ''
        fields=[('Titel',order.get('title',''),False),('Fällig (JJJJ-MM-TT SS:MM, optional)',due,False),
                ('Wiederholung in Tagen (0 = keine)',order.get('recurrenceDays',0),False)]
        values=self.form('Wartungsauftrag bearbeiten' if order else 'Wartungsauftrag anlegen',fields)
        if values is None:return
        try:
            when=self.parse_local_time(values[1]) if values[1] else 0;repeat=int(values[2])
            if not 0<=repeat<=3660:raise ValueError()
        except (ValueError,OverflowError,OSError):self.message('Bitte gültige Fälligkeit und Wiederholung eingeben.');return
        args=['-action',('maintenance-save' if self.local_mode else ('central-maintenance-update' if order else 'central-maintenance-create')),'-asset-id',str(asset['id']),'-id',str(order.get('id','')),
              '-title',values[0],'-status',order.get('status','open'),'-due',str(when),'-recurrence',str(repeat)]
        if self.local_mode and not order:args[5]='0'
        self.local_facility_action(args,lambda:self.show_maintenance(asset))

    def archive_facility(self,kind,item,asset=None):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Eintrag archivieren?')
        dialog.format_secondary_text(item.get('name',item.get('title','')));dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response!=Gtk.ResponseType.OK:return
        if kind=='asset':
            args=['-action','asset-archive','-id',str(item['id'])] if self.local_mode else ['-action','archive-facility-asset','-id',str(item['id']),'-name',item['name'],'-asset-category',item['category'],'-location',item['location'],'-asset-status',item['status']]
            self.local_facility_action(args,self.show_assets)
        else:self.local_facility_action(['-action','maintenance-archive' if self.local_mode else 'central-maintenance-archive','-asset-id',str(asset['id']),'-id',str(item['id'])],lambda:self.show_maintenance(asset))

    def choose_system_role(self,employee):
        generation=self.generation
        def loaded(roles,error):
            if generation!=self.generation or not self.local_mode:return
            if error:self.message(error);return
            if not roles:self.message('Keine aktiven Rollen vorhanden.');return
            dialog=Gtk.Dialog(title='Systemrolle zuweisen',transient_for=self,modal=True)
            dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Zuweisen',Gtk.ResponseType.OK)
            content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
            content.pack_start(label(employee['name']),False,False,0);choice=Gtk.ComboBoxText()
            for role in roles:choice.append(role['code'],role['name']+' · '+role['code'])
            if not choice.set_active_id(employee.get('systemRole','')):choice.set_active(-1)
            content.pack_start(choice,False,False,0);dialog.show_all();response=dialog.run();selected=choice.get_active_id();dialog.destroy()
            if response==Gtk.ResponseType.OK and not selected:self.message('Bitte eine Systemrolle auswählen.');return
            if response==Gtk.ResponseType.OK and selected:
                self.run_client(['-action','assign-role','-id',str(employee['id']),'-role',selected],lambda _value,err:self.finished(err))
        self.run_client(['-action','roles'],loaded)

    def show_roles(self):
        if not self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Rollen werden geladen …','muted'),False,False,0);self.render()
        def loaded(roles,error):
            if generation!=self.generation or not self.local_mode:return
            self.clear();header=box(False,8);header.pack_start(label('Systemrollen','section'),True,True,0)
            header.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0)
            header.pack_start(button('+ Rolle',lambda _:self.role_form(),'primary'),False,False,0);self.content.pack_start(header,False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for role in roles or []:
                details=role['code']+' · '+', '.join(role['permissions'])
                actions=[] if role['code']=='admin' else [('Bearbeiten',lambda _b,v=role:self.role_form(v)),('Archivieren',lambda _b,v=role:self.archive_role(v))]
                self.row(role['name'],details,extra=actions)
            if roles==[]:self.content.pack_start(label('Keine Rollen vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','roles'],loaded)

    def role_action(self,args):
        generation=self.generation
        def done(_value,error):
            if generation!=self.generation or not self.local_mode:return
            if error:self.message(error)
            else:self.show_roles()
        self.run_client(args,done)

    def role_form(self,role=None):
        role=role or {}
        fields=[('Code',role.get('code',''),False),('Name',role.get('name',''),False),
                ('Beschreibung',role.get('description',''),False),('Berechtigungen (kommagetrennt)',', '.join(role.get('permissions',[])),False)]
        values=self.form('Rolle bearbeiten' if role else 'Rolle anlegen',fields)
        if values is None:return
        self.role_action(['-action','role-save','-id',str(role.get('id',0)),'-code',values[0],'-name',values[1],'-description',values[2],'-permissions',values[3]])

    def archive_role(self,role):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Rolle archivieren?')
        dialog.format_secondary_text('Zugewiesene Konten verlieren die Berechtigungen dieser Rolle: '+role['name'])
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:self.role_action(['-action','role-archive','-id',str(role['id'])])

    def save_database_backup(self):
        if not self.local_mode:return
        d=Gtk.FileChooserDialog(title='Datenbank sichern',transient_for=self,action=Gtk.FileChooserAction.SAVE)
        d.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Sichern',Gtk.ResponseType.OK)
        d.set_current_name('aura-sicherung.sqlite')
        response=d.run();path=d.get_filename();d.destroy()
        if response!=Gtk.ResponseType.OK or not path:return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Datenbank sichern?')
        confirm.format_secondary_text('Nur die Datenbank wird gesichert. Anhänge müssen separat gesichert werden. Eine bestehende Datei wird nicht überschrieben.')
        confirm.add_button('Abbrechen',Gtk.ResponseType.CANCEL);confirm.add_button('Sichern',Gtk.ResponseType.OK)
        response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        def done(result,error):
            if error:self.message(error);return
            info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Datenbank gesichert')
            info.format_secondary_text(path);info.run();info.destroy()
        self.run_client(['-action','backup','-path',path],done)

    def on_window_destroy(self,_window):
        if self.dashboard_source:GLib.source_remove(self.dashboard_source);self.dashboard_source=0
        self.window_closed=True;self.stop_room_auto_sync();self.stop_booking_auto_sync();self.stop_employee_auto_sync();self.stop_presence_auto_sync(clear=True);self.stop_leave_auto_sync();self.stop_approval_auto_sync();self.stop_task_auto_sync();self.stop_project_auto_sync();self.stop_assignment_auto_sync();self.stop_comment_auto_sync();self.stop_ticket_auto_sync();self.stop_ticket_comment_auto_sync();self.stop_attachment_auto_sync('task');self.stop_attachment_auto_sync('ticket');self.stop_organization_auto_sync();self.stop_facility_auto_sync('assets');self.stop_facility_auto_sync('maintenance')

    def stop_room_auto_sync(self):
        self.room_auto_enabled=False;self.room_auto_session=None
        if self.room_auto_source:
            GLib.source_remove(self.room_auto_source);self.room_auto_source=0

    def toggle_room_auto_sync(self,widget):
        if not widget.get_active():self.stop_room_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Raumabgleich einmal manuell einrichten.');return
        try:
            with self.local_lock:config=self.local.next_room_sync()
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Raumabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.room_auto_enabled=True;self.room_auto_session=self.local;self.room_auto_error=''
        if self.tab==5 and self.room_auto_notice:self.room_auto_notice.set_text('')
        self.room_auto_source=GLib.timeout_add_seconds(self.room_auto_interval,self.room_auto_tick)
        self.room_auto_tick()

    def room_auto_tick(self):
        if self.window_closed or not self.room_auto_enabled or not self.local_mode or self.local is not self.room_auto_session:
            self.room_auto_source=0;return False
        if self.room_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.room_sync_running=True;self.state.set_text('Automatischer Raumabgleich …')
        def work():
            try:
                result=sync_rooms(session,self.local_lock,lambda:self.local is session and self.local_mode and self.room_auto_enabled and self.room_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable
            def done():
                self.room_sync_running=False
                if self.window_closed or not self.room_auto_enabled or self.local is not session or self.room_auto_session is not session:return False
                if error:
                    if retryable:
                        self.room_auto_error='Automatischer Raumabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.'
                        self.state.set_text('Raumabgleich wartet auf Server')
                    else:
                        self.stop_room_auto_sync();self.room_auto_error='Automatischer Raumabgleich angehalten: '+error
                        self.state.set_text('Raumabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.room_auto_error='';self.state.set_text('Raumabgleich automatisch aktiv')
                    if self.tab==5 and self.room_auto_notice:self.room_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,1):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def start_room_sync(self):
        if not self.local_mode:return
        if self.room_sync_running:self.message('Ein Raumabgleich läuft bereits.');return
        values=self.form('Raumübertragung zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Lokale Räume synchronisieren?')
        confirm.format_secondary_text('Räume und spätere Raumänderungen werden an dieses Serverkonto gebunden und manuell mit dem Server abgeglichen. Dieser Raumabgleich überträgt keine Buchungen, Mitarbeiter, Urlaub oder andere Daten. Bei Konflikten bleibt der Abgleich stehen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übertragen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca
        session=self.local;server_url=self.url;server_token=self.token;server_ca=self.ca;self.room_sync_running=True;self.state.set_text('Räume werden abgeglichen …')
        def work():
            try:sent=sync_rooms(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,server_url,server_token,server_ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:sent=0;error=str(exc)
            def finished():
                self.room_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Raumabgleich angehalten');self.message(error+'\nDie ausstehenden Änderungen bleiben in der Warteschlange.')
                else:
                    self.room_auto_error='';self.state.set_text('Raumabgleich abgeschlossen')
                    if self.tab==5 and self.room_auto_notice:self.room_auto_notice.set_text('')
                    d=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Raumabgleich abgeschlossen')
                    d.format_secondary_text(str(sent['sent'])+' Änderungen übertragen, '+str(sent['received'])+' Räume übernommen. Andere lokale Daten wurden nicht synchronisiert.');d.run();d.destroy()
                return False
            GLib.idle_add(finished)
        threading.Thread(target=work,daemon=True).start()

    def start_personal_presence_sync(self):
        if not self.local_mode:return
        if self.employee_sync_running:self.message('Ein Mitarbeiterabgleich läuft bereits.');return
        session=self.local
        saved=self.presence_auto_credentials or (self.url,self.token,self.ca)
        values=self.form('Eigene Präsenz synchronisieren',[('Server-URL',saved[0],False),('Persönliches Server-Token',saved[1],True),('CA-Zertifikat (optional)',saved[2],False)],'Abgleichen')
        if values is None:return
        if not self.local_mode or self.local is not session:self.message('Die lokale Sitzung wurde gewechselt.');return
        if self.employee_sync_running:self.message('Ein Mitarbeiterabgleich läuft bereits.');return
        url,token,ca=values;self.employee_sync_running=True;self.state.set_text('Eigene Präsenz wird abgeglichen …')
        def work():
            try:result=sync_personal_presence(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.employee_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Präsenzabgleich angehalten');self.message(error);return False
                self.presence_auto_credentials=(url.strip().rstrip('/'),token,ca);self.presence_auto_error=''
                self.state.set_text('Präsenzabgleich abgeschlossen');self.message(str(result['sent'])+' eigene Präsenzänderungen übertragen, '+str(result['received'])+' Serverstatus übernommen.');self.refresh();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def stop_presence_auto_sync(self,clear=False):
        self.presence_auto_enabled=False;self.presence_auto_session=None
        if self.presence_auto_source:GLib.source_remove(self.presence_auto_source);self.presence_auto_source=0
        if clear:self.presence_auto_credentials=None;self.presence_auto_error=''

    def toggle_presence_auto_sync(self,widget):
        if not widget.get_active():self.stop_presence_auto_sync();return
        if not self.local_mode or not self.local or not self.presence_auto_credentials:
            widget.set_active(False);self.message('Bitte den persönlichen Präsenzabgleich in dieser Sitzung zuerst manuell durchführen.');return
        self.presence_auto_enabled=True;self.presence_auto_session=self.local;self.presence_auto_error=''
        if self.presence_auto_notice:self.presence_auto_notice.set_text('')
        self.presence_auto_source=GLib.timeout_add_seconds(self.presence_auto_interval,self.presence_auto_tick);self.presence_auto_tick()

    def presence_auto_tick(self):
        if self.window_closed or not self.presence_auto_enabled or not self.local_mode or self.local is not self.presence_auto_session:
            self.presence_auto_source=0;return False
        if self.employee_sync_running:return True
        session=self.local;credentials=self.presence_auto_credentials;self.employee_sync_running=True;self.state.set_text('Automatischer Präsenzabgleich …')
        def work():
            try:
                result=sync_personal_presence(session,self.local_lock,lambda:self.local is session and self.local_mode and self.presence_auto_enabled and self.presence_auto_session is session and self.presence_auto_credentials==credentials,CLIENT,*credentials);error=None;retryable=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable
            def done():
                self.employee_sync_running=False
                if self.window_closed or not self.presence_auto_enabled or self.local is not session or self.presence_auto_session is not session or self.presence_auto_credentials!=credentials:return False
                if error:
                    if retryable:self.presence_auto_error='Präsenzabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Präsenzabgleich wartet auf Server')
                    else:self.stop_presence_auto_sync();self.presence_auto_error='Präsenzabgleich angehalten: '+error;self.state.set_text('Präsenzabgleich angehalten · bitte prüfen')
                    if self.presence_auto_notice and self.presence_auto_notice.get_parent() is self.content:self.refresh()
                else:
                    self.presence_auto_error='';self.state.set_text('Präsenzabgleich automatisch aktiv')
                    if self.presence_auto_notice and self.presence_auto_notice.get_parent() is self.content:self.presence_auto_notice.set_text('')
                    if result['received'] or result['sent']:
                        if self.presence_auto_notice and self.presence_auto_notice.get_parent() is self.content:self.refresh()
                        else:self.update_own_presence()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_employee_auto_sync(self):
        self.employee_auto_enabled=False;self.employee_auto_session=None
        if self.employee_auto_source:
            GLib.source_remove(self.employee_auto_source);self.employee_auto_source=0
        if self.employee_auto_retry:
            GLib.source_remove(self.employee_auto_retry);self.employee_auto_retry=0

    def toggle_employee_auto_sync(self,widget):
        if not widget.get_active():self.stop_employee_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Mitarbeiterabgleich einmal manuell einrichten.');return
        try:
            session=self.local
            config=employee_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Mitarbeiterabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.employee_auto_enabled=True;self.employee_auto_session=self.local;self.employee_auto_error=''
        if self.tab==5 and self.employee_auto_notice:self.employee_auto_notice.set_text('')
        self.employee_auto_source=GLib.timeout_add_seconds(self.employee_auto_interval,self.employee_auto_tick)
        self.employee_auto_tick()

    def employee_auto_tick(self):
        if self.window_closed or not self.employee_auto_enabled or not self.local_mode or self.local is not self.employee_auto_session:
            self.employee_auto_source=0;return False
        if self.employee_sync_running:
            if self.organization_sync_running and not self.employee_auto_retry:
                def retry():
                    self.employee_auto_retry=0
                    if self.employee_auto_enabled:self.employee_auto_tick()
                    return False
                self.employee_auto_retry=GLib.timeout_add(500,retry)
            return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.employee_sync_running=True;self.state.set_text('Automatischer Mitarbeiterabgleich …')
        def work():
            try:
                result=sync_employees(session,self.local_lock,lambda:self.local is session and self.local_mode and self.employee_auto_enabled and self.employee_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable
            def done():
                self.employee_sync_running=False
                if self.window_closed or not self.employee_auto_enabled or self.local is not session or self.employee_auto_session is not session:return False
                if error:
                    if retryable:
                        self.employee_auto_error='Automatischer Mitarbeiterabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Mitarbeiterabgleich wartet auf Server')
                    else:
                        self.stop_employee_auto_sync();self.employee_auto_error='Automatischer Mitarbeiterabgleich angehalten: '+error;self.state.set_text('Mitarbeiterabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.employee_auto_error='';self.state.set_text('Mitarbeiterabgleich automatisch aktiv')
                    if self.tab==5 and self.employee_auto_notice:self.employee_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,3):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_project_auto_sync(self):
        self.project_auto_enabled=False;self.project_auto_session=None
        if self.project_auto_source:
            GLib.source_remove(self.project_auto_source);self.project_auto_source=0

    def toggle_project_auto_sync(self,widget):
        if not widget.get_active():self.stop_project_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Projektabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=project_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Projektabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.project_auto_enabled=True;self.project_auto_session=self.local;self.project_auto_error=''
        if self.tab==5 and self.project_auto_notice:self.project_auto_notice.set_text('')
        self.project_auto_source=GLib.timeout_add_seconds(self.project_auto_interval,self.project_auto_tick);self.project_auto_tick()

    def project_auto_tick(self):
        if self.window_closed or not self.project_auto_enabled or not self.local_mode or self.local is not self.project_auto_session:
            self.project_auto_source=0;return False
        if self.project_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.project_sync_running=True;self.state.set_text('Automatischer Projektabgleich …')
        def work():
            try:
                result=sync_projects(session,self.local_lock,lambda:self.local is session and self.local_mode and self.project_auto_enabled and self.project_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,ProjectOwnerPendingError)
                if pending:result={'received':exc.received}
            def done():
                self.project_sync_running=False
                if self.window_closed or not self.project_auto_enabled or self.local is not session or self.project_auto_session is not session:return False
                if error:
                    if pending:self.project_auto_error='Automatischer Projektabgleich wartet auf die Zuordnung einer Projektleitung.';self.state.set_text('Projektabgleich wartet auf Leitung')
                    elif retryable:self.project_auto_error='Automatischer Projektabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Projektabgleich wartet auf Server')
                    else:self.stop_project_auto_sync();self.project_auto_error='Automatischer Projektabgleich angehalten: '+error;self.state.set_text('Projektabgleich angehalten · bitte prüfen')
                    if self.tab==5 or (pending and result and result['received'] and self.tab in (0,2)):self.refresh()
                else:
                    self.project_auto_error='';self.state.set_text('Projektabgleich automatisch aktiv')
                    if self.tab==5 and self.project_auto_notice:self.project_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def start_project_sync(self):
        if not self.local_mode:return
        if self.project_sync_running:self.message('Ein Projektabgleich läuft bereits.');return
        values=self.form('Projektabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Lokale Projekte synchronisieren?')
        confirm.format_secondary_text('Projekte und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden. Projektleitungen müssen durch den Mitarbeiterabgleich eindeutig einem Serverkonto zugeordnet sein. Projekt-Aufgaben-Zuordnungen können anschließend separat abgeglichen werden. Bei Konflikten bleibt der Abgleich stehen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.project_sync_running=True;self.state.set_text('Projekte werden abgeglichen …')
        def work():
            try:result=sync_projects(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.project_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Projektabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.project_auto_error='';self.state.set_text('Projektabgleich abgeschlossen')
                if self.tab==5 and self.project_auto_notice:self.project_auto_notice.set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Projektabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Projekte übernommen. Aufgaben-Zuordnungen können separat abgeglichen werden.');dialog.run();dialog.destroy()
                if self.tab==2:self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def stop_assignment_auto_sync(self):
        self.assignment_auto_enabled=False;self.assignment_auto_session=None
        if self.assignment_auto_source:
            GLib.source_remove(self.assignment_auto_source);self.assignment_auto_source=0

    def toggle_assignment_auto_sync(self,widget):
        if not widget.get_active():self.stop_assignment_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Zuordnungsabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=assignment_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Zuordnungsabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.assignment_auto_enabled=True;self.assignment_auto_session=self.local;self.assignment_auto_error=''
        if self.tab==5 and self.assignment_auto_notice:self.assignment_auto_notice.set_text('')
        self.assignment_auto_source=GLib.timeout_add_seconds(self.assignment_auto_interval,self.assignment_auto_tick);self.assignment_auto_tick()

    def assignment_auto_tick(self):
        if self.window_closed or not self.assignment_auto_enabled or not self.local_mode or self.local is not self.assignment_auto_session:
            self.assignment_auto_source=0;return False
        if self.project_sync_running or self.task_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.project_sync_running=True;self.task_sync_running=True;self.state.set_text('Automatischer Projektzuordnungsabgleich …')
        def work():
            try:
                result=sync_project_assignments(session,self.local_lock,lambda:self.local is session and self.local_mode and self.assignment_auto_enabled and self.assignment_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;parents_pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;parents_pending=isinstance(exc,AssignmentParentsPendingError)
            def done():
                self.project_sync_running=False;self.task_sync_running=False
                if self.window_closed or not self.assignment_auto_enabled or self.local is not session or self.assignment_auto_session is not session:return False
                if error:
                    if parents_pending:self.assignment_auto_error='Projektzuordnungen warten auf den Abgleich der zugehörigen Projekte und Aufgaben.';self.state.set_text('Projektzuordnungen warten auf Projekte und Aufgaben')
                    elif retryable:self.assignment_auto_error='Automatischer Zuordnungsabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Projektzuordnungen warten auf Server')
                    else:self.stop_assignment_auto_sync();self.assignment_auto_error='Automatischer Zuordnungsabgleich angehalten: '+error;self.state.set_text('Projektzuordnungsabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.assignment_auto_error='';self.state.set_text('Projektzuordnungsabgleich automatisch aktiv')
                    if self.tab==5 and self.assignment_auto_notice:self.assignment_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def start_assignment_sync(self):
        if not self.local_mode:return
        if self.project_sync_running or self.task_sync_running:self.message('Bitte warten Sie, bis der Aufgaben- oder Projektabgleich beendet ist.');return
        values=self.form('Projektzuordnungen abgleichen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Projektzuordnungen synchronisieren?')
        confirm.format_secondary_text('Zuerst müssen Projekt- und Aufgabenabgleich für dasselbe Serverkonto eingerichtet sein. Lokale Zuordnungen und spätere Änderungen werden an diesen Server gebunden. Bei Konflikten bleiben Änderungen gespeichert.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.project_sync_running=True;self.task_sync_running=True;self.state.set_text('Projektzuordnungen werden abgeglichen …')
        def work():
            try:result=sync_project_assignments(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.project_sync_running=False;self.task_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Projektzuordnungsabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.state.set_text('Projektzuordnungen abgeglichen')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Projektzuordnungen abgeglichen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Änderungen übernommen.');dialog.run();dialog.destroy()
                if self.tab==2:self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_assignment_sync_conflict(self):
        if not self.local_mode:return
        if self.project_sync_running or self.task_sync_running:self.message('Bitte warten Sie, bis der Aufgaben- oder Projektabgleich beendet ist.');return
        values=self.form('Projektzuordnungskonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.project_sync_running=True;self.task_sync_running=True;self.state.set_text('Projektzuordnungskonflikt wird geprüft …')
        def release():self.project_sync_running=False;self.task_sync_running=False
        def work():
            try:result=inspect_assignment_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:release();return False
                if error:release();self.state.set_text('Projektzuordnungskonflikt nicht geprüft');self.message(error);return False
                p=result['preview'];dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Projektzuordnungskonflikt entscheiden')
                dialog.format_secondary_text('Projekt: '+p['projectName']+'\nAufgabe: '+p['taskTitle']+'\n\nLokal zugeordnet: '+('ja' if p['localPresent'] else 'nein')+'\nServerseitig zugeordnet: '+('ja' if result['remotePresent'] else 'nein')+'\n\nDie bisherigen Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):release();self.state.set_text('Projektzuordnungskonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                def apply():
                    try:resolve_assignment_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        release()
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Projektzuordnungskonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Projektzuordnungskonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverstand übernommen.' if accept else 'Lokaler Stand für den nächsten Abgleich vorgemerkt.')
                        info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def inspect_project_sync_conflict(self):
        if not self.local_mode:return
        if self.project_sync_running:self.message('Ein Projektabgleich läuft bereits.');return
        values=self.form('Projektkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.project_sync_running=True;self.state.set_text('Projektkonflikt wird geprüft …')
        def work():
            try:result=inspect_project_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.project_sync_running=False;return False
                if error:self.project_sync_running=False;self.state.set_text('Projektkonflikt nicht geprüft');self.message(error);return False
                def describe(v):return ('Name: '+v['name']+'\nBereich: '+v['department']+'\nBeschreibung: '+v['description']+'\nStatus: '+v['status']+'\nServerkonto der Leitung: '+str(v['ownerId'])+'\nAktiv: '+('ja' if v['active'] else 'nein'))
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Projektkonflikt entscheiden')
                dialog.format_secondary_text('Lokal:\n'+describe(result['preview']['localWire'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie bisherigen '+str(len(result['preview']['pending']))+' Änderungen werden protokolliert. Aufgaben-Zuordnungen bleiben bei dieser Entscheidung unverändert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.project_sync_running=False;self.state.set_text('Projektkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if accept:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serverprojekt lokal übernehmen?')
                    confirm.format_secondary_text('Der geprüfte Serverstand ersetzt dieses lokale Projekt. Die ausstehenden Änderungen werden aus der Warteschlange entfernt und protokolliert; lokale Aufgaben-Zuordnungen bleiben bestehen.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.project_sync_running=False;self.state.set_text('Projektkonflikt unverändert');return False
                def apply():
                    try:resolve_project_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.project_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Projektkonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Projektkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverprojekt übernommen' if accept else 'Lokales Projekt vorgemerkt')
                        info.format_secondary_text('Der geprüfte Serverstand wurde lokal übernommen.' if accept else 'Der Serverstand wird beim nächsten Abgleich erneut geprüft.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def stop_task_auto_sync(self):
        self.task_auto_enabled=False;self.task_auto_session=None
        if self.task_auto_source:
            GLib.source_remove(self.task_auto_source);self.task_auto_source=0

    def toggle_task_auto_sync(self,widget):
        if not widget.get_active():self.stop_task_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Aufgabenabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=task_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Aufgabenabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.task_auto_enabled=True;self.task_auto_session=self.local;self.task_auto_error=''
        if self.tab==5 and self.task_auto_notice:self.task_auto_notice.set_text('')
        self.task_auto_source=GLib.timeout_add_seconds(self.task_auto_interval,self.task_auto_tick);self.task_auto_tick()

    def task_auto_tick(self):
        if self.window_closed or not self.task_auto_enabled or not self.local_mode or self.local is not self.task_auto_session:
            self.task_auto_source=0;return False
        if self.task_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.task_sync_running=True;self.state.set_text('Automatischer Aufgabenabgleich …')
        def work():
            try:
                result=sync_tasks(session,self.local_lock,lambda:self.local is session and self.local_mode and self.task_auto_enabled and self.task_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,TaskAssigneePendingError)
                if pending:result={'received':exc.received}
            def done():
                self.task_sync_running=False
                if self.window_closed or not self.task_auto_enabled or self.local is not session or self.task_auto_session is not session:return False
                if error:
                    if pending:self.task_auto_error='Automatischer Aufgabenabgleich wartet auf die Zuordnung einer zuständigen Person.';self.state.set_text('Aufgabenabgleich wartet auf Zuordnung')
                    elif retryable:self.task_auto_error='Automatischer Aufgabenabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Aufgabenabgleich wartet auf Server')
                    else:self.stop_task_auto_sync();self.task_auto_error='Automatischer Aufgabenabgleich angehalten: '+error;self.state.set_text('Aufgabenabgleich angehalten · bitte prüfen')
                    if self.tab==5 or (pending and result and result['received'] and self.tab in (0,2)):self.refresh()
                else:
                    self.task_auto_error='';self.state.set_text('Aufgabenabgleich automatisch aktiv')
                    if self.tab==5 and self.task_auto_notice:self.task_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_comment_auto_sync(self):
        self.comment_auto_enabled=False;self.comment_auto_session=None
        if self.comment_auto_source:
            GLib.source_remove(self.comment_auto_source);self.comment_auto_source=0

    def toggle_comment_auto_sync(self,widget):
        if not widget.get_active():self.stop_comment_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Kommentarabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=task_comment_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Kommentarabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.comment_auto_enabled=True;self.comment_auto_session=self.local;self.comment_auto_error=''
        if self.tab==5 and self.comment_auto_notice:self.comment_auto_notice.set_text('')
        self.comment_auto_source=GLib.timeout_add_seconds(self.comment_auto_interval,self.comment_auto_tick);self.comment_auto_tick()

    def comment_auto_tick(self):
        if self.window_closed or not self.comment_auto_enabled or not self.local_mode or self.local is not self.comment_auto_session:
            self.comment_auto_source=0;return False
        if self.task_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.task_sync_running=True;self.state.set_text('Automatischer Kommentarabgleich …')
        def work():
            try:
                result=sync_task_comments(session,self.local_lock,lambda:self.local is session and self.local_mode and self.comment_auto_enabled and self.comment_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;task_pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;task_pending=isinstance(exc,CommentTaskPendingError)
            def done():
                self.task_sync_running=False
                if self.window_closed or not self.comment_auto_enabled or self.local is not session or self.comment_auto_session is not session:return False
                if error:
                    if task_pending:self.comment_auto_error='Kommentare warten auf den Abgleich der zugehörigen Aufgabe.';self.state.set_text('Kommentare warten auf Aufgabenabgleich')
                    elif retryable:self.comment_auto_error='Automatischer Kommentarabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Kommentarabgleich wartet auf Server')
                    else:self.stop_comment_auto_sync();self.comment_auto_error='Automatischer Kommentarabgleich angehalten: '+error;self.state.set_text('Kommentarabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.comment_auto_error='';self.state.set_text('Kommentarabgleich automatisch aktiv')
                    if self.tab==5 and self.comment_auto_notice:self.comment_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_ticket_auto_sync(self):
        self.ticket_auto_enabled=False;self.ticket_auto_session=None;self.ticket_auto_waiting=False
        if self.ticket_auto_source:
            GLib.source_remove(self.ticket_auto_source);self.ticket_auto_source=0

    def toggle_ticket_auto_sync(self,widget):
        if not widget.get_active():self.stop_ticket_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Ticketabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=ticket_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Ticketabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.ticket_auto_enabled=True;self.ticket_auto_session=self.local;self.ticket_auto_error=''
        if self.tab==5 and self.ticket_auto_notice:self.ticket_auto_notice.set_text('')
        self.ticket_auto_source=GLib.timeout_add_seconds(self.ticket_auto_interval,self.ticket_auto_tick);self.ticket_auto_tick()

    def ticket_auto_tick(self):
        if self.window_closed or not self.ticket_auto_enabled or not self.local_mode or self.local is not self.ticket_auto_session:
            self.ticket_auto_source=0;return False
        if self.ticket_sync_running:self.ticket_auto_waiting=True;return True
        self.ticket_auto_waiting=False
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.ticket_sync_running=True;self.state.set_text('Automatischer Ticketabgleich …')
        def work():
            try:
                result=sync_tickets(session,self.local_lock,lambda:self.local is session and self.local_mode and self.ticket_auto_enabled and self.ticket_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,TicketAssigneePendingError)
                if pending:result={'received':exc.received}
            def done():
                self.ticket_sync_running=False
                if self.ticket_comment_auto_waiting and self.ticket_comment_auto_enabled:
                    self.ticket_comment_auto_waiting=False;GLib.idle_add(lambda:(self.ticket_comment_auto_tick(),False)[1])
                if self.window_closed or not self.ticket_auto_enabled or self.local is not session or self.ticket_auto_session is not session:return False
                if error:
                    if pending:self.ticket_auto_error='Automatischer Ticketabgleich wartet auf die Zuordnung einer zuständigen Person.';self.state.set_text('Ticketabgleich wartet auf Zuordnung')
                    elif retryable:self.ticket_auto_error='Automatischer Ticketabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Ticketabgleich wartet auf Server')
                    else:self.stop_ticket_auto_sync();self.ticket_auto_error='Automatischer Ticketabgleich angehalten: '+error;self.state.set_text('Ticketabgleich angehalten · bitte prüfen')
                    if self.tab==5 or (pending and result and result['received'] and self.tab in (0,4)):self.refresh()
                else:
                    self.ticket_auto_error='';self.state.set_text('Ticketabgleich automatisch aktiv')
                    if self.tab==5 and self.ticket_auto_notice:self.ticket_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,4):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_ticket_comment_auto_sync(self):
        self.ticket_comment_auto_enabled=False;self.ticket_comment_auto_session=None;self.ticket_comment_auto_waiting=False
        if self.ticket_comment_auto_source:
            GLib.source_remove(self.ticket_comment_auto_source);self.ticket_comment_auto_source=0

    def toggle_ticket_comment_auto_sync(self,widget):
        if not widget.get_active():self.stop_ticket_comment_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Ticketkommentarabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=ticket_comment_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Ticketkommentarabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.ticket_comment_auto_enabled=True;self.ticket_comment_auto_session=self.local;self.ticket_comment_auto_error=''
        if self.tab==5 and self.ticket_comment_auto_notice:self.ticket_comment_auto_notice.set_text('')
        self.ticket_comment_auto_source=GLib.timeout_add_seconds(self.ticket_comment_auto_interval,self.ticket_comment_auto_tick);self.ticket_comment_auto_tick()

    def ticket_comment_auto_tick(self):
        if self.window_closed or not self.ticket_comment_auto_enabled or not self.local_mode or self.local is not self.ticket_comment_auto_session:
            self.ticket_comment_auto_source=0;return False
        if self.ticket_sync_running:self.ticket_comment_auto_waiting=True;return True
        self.ticket_comment_auto_waiting=False
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.ticket_sync_running=True;self.state.set_text('Automatischer Ticketkommentarabgleich …')
        def work():
            try:
                result=sync_ticket_comments(session,self.local_lock,lambda:self.local is session and self.local_mode and self.ticket_comment_auto_enabled and self.ticket_comment_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;task_pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;task_pending=isinstance(exc,CommentTicketPendingError)
            def done():
                self.ticket_sync_running=False
                if self.ticket_auto_waiting and self.ticket_auto_enabled:
                    self.ticket_auto_waiting=False;GLib.idle_add(lambda:(self.ticket_auto_tick(),False)[1])
                if self.window_closed or not self.ticket_comment_auto_enabled or self.local is not session or self.ticket_comment_auto_session is not session:return False
                if error:
                    if task_pending:self.ticket_comment_auto_error='Kommentare warten auf den Abgleich der zugehörigen Tickets.';self.state.set_text('Kommentare warten auf Ticketabgleich')
                    elif retryable:self.ticket_comment_auto_error='Automatischer Ticketkommentarabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Ticketkommentarabgleich wartet auf Server')
                    else:self.stop_ticket_comment_auto_sync();self.ticket_comment_auto_error='Automatischer Ticketkommentarabgleich angehalten: '+error;self.state.set_text('Ticketkommentarabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.ticket_comment_auto_error='';self.state.set_text('Ticketkommentarabgleich automatisch aktiv')
                    if self.tab==5 and self.ticket_comment_auto_notice:self.ticket_comment_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def stop_organization_auto_sync(self):
        self.organization_auto_enabled=False;self.organization_auto_session=None
        for key in ('organization_auto_source','organization_auto_retry'):
            source=getattr(self,key)
            if source:GLib.source_remove(source);setattr(self,key,0)

    def toggle_organization_auto_sync(self,widget):
        if not widget.get_active():self.stop_organization_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Organisationsabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=organization_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Organisationsabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.organization_auto_enabled=True;self.organization_auto_session=self.local;self.organization_auto_error=''
        if self.tab==5 and self.organization_auto_notice:self.organization_auto_notice.set_text('')
        self.organization_auto_source=GLib.timeout_add_seconds(self.organization_auto_interval,self.organization_auto_tick);self.organization_auto_tick()

    def organization_auto_tick(self):
        if self.window_closed or not self.organization_auto_enabled or not self.local_mode or self.local is not self.organization_auto_session:
            self.organization_auto_source=0;return False
        if self.organization_sync_running:return True
        if self.employee_sync_running:
            if not self.organization_auto_retry:
                def retry():
                    self.organization_auto_retry=0
                    if self.organization_auto_enabled:self.organization_auto_tick()
                    return False
                self.organization_auto_retry=GLib.timeout_add(500,retry)
            return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.organization_sync_running=True;self.employee_sync_running=True;self.state.set_text('Automatischer Organisationsabgleich …')
        def work():
            try:
                result=sync_organizations(session,self.local_lock,lambda:self.local is session and self.local_mode and self.organization_auto_enabled and self.organization_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,OrganizationManagerPendingError)
            def done():
                self.organization_sync_running=False;self.employee_sync_running=False
                if self.window_closed or not self.organization_auto_enabled or self.local is not session or self.organization_auto_session is not session:return False
                if error:
                    if pending:self.organization_auto_error='Organisationsleitung wartet auf den Mitarbeiterabgleich.';self.state.set_text('Organisationsabgleich wartet auf Mitarbeiter')
                    elif retryable:self.organization_auto_error='Automatischer Organisationsabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Organisationsabgleich wartet auf Server')
                    else:self.stop_organization_auto_sync();self.organization_auto_error='Automatischer Organisationsabgleich angehalten: '+error;self.state.set_text('Organisationsabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    self.organization_auto_error='';self.state.set_text('Organisationsabgleich automatisch aktiv')
                    if self.tab==5 and self.organization_auto_notice:self.organization_auto_notice.set_text('')
                    if result['received'] and self.tab in (0,3):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def start_organization_sync(self):
        if not self.local_mode:return
        if self.organization_sync_running or self.employee_sync_running:self.message('Ein Organisations- oder Mitarbeiterabgleich läuft bereits.');return
        values=self.form('Organisationsabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Organisationseinheiten synchronisieren?')
        confirm.format_secondary_text('Organisationseinheiten und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden. Zugeordnete Leitungen benötigen einen bestätigten Mitarbeiterabgleich; Einheiten ohne Leitung sind zulässig. Bei Konflikten bleiben Änderungen gespeichert.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.organization_sync_running=True;self.employee_sync_running=True;self.state.set_text('Organisationseinheiten werden abgeglichen …')
        def work():
            try:result=sync_organizations(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.organization_sync_running=False;self.employee_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Organisationsabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.organization_auto_error='';self.state.set_text('Organisationsabgleich abgeschlossen')
                if self.tab==5 and self.organization_auto_notice:self.organization_auto_notice.set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Organisationsabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Organisationseinheiten übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_organization_sync_conflict(self):
        if not self.local_mode:return
        if self.organization_sync_running or self.employee_sync_running:self.message('Ein Organisations- oder Mitarbeiterabgleich läuft bereits.');return
        values=self.form('Organisationskonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.organization_sync_running=True;self.employee_sync_running=True;self.state.set_text('Organisationskonflikt wird geprüft …')
        def work():
            try:result=inspect_organization_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.organization_sync_running=False;self.employee_sync_running=False;return False
                if error:self.organization_sync_running=False;self.employee_sync_running=False;self.state.set_text('Organisationskonflikt nicht geprüft');self.message(error);return False
                def describe(v):return 'Name: '+v['name']+'\nCode: '+v['code']+'\nServerkonto der Leitung: '+str(v['managerId'])+'\nAktiv: '+('ja' if v['active'] else 'nein')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Organisationskonflikt entscheiden')
                dialog.format_secondary_text('Lokal:\n'+describe(result['preview']['localWire'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie bisherigen Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.organization_sync_running=False;self.employee_sync_running=False;self.state.set_text('Organisationskonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                def apply():
                    try:resolve_organization_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.organization_sync_running=False;self.employee_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Organisationskonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Organisationskonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverorganisation übernommen' if accept else 'Lokale Organisation vorgemerkt');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def stop_facility_auto_sync(self,kind):
        state=self.facility_auto[kind];state['enabled']=False;state['session']=None
        for key in ('source','retry'):
            if state[key]:GLib.source_remove(state[key]);state[key]=0

    def toggle_facility_auto_sync(self,widget,kind):
        if not widget.get_active():self.stop_facility_auto_sync(kind);return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und diesen Abgleich einmal manuell einrichten.');return
        try:
            session=self.local;config_fn=facility_asset_sync_config if kind=='assets' else maintenance_sync_config
            config=config_fn(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte diesen Abgleich für das Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        state=self.facility_auto[kind];state['enabled']=True;state['session']=self.local;state['error']=''
        if self.tab==5 and state['notice']:state['notice'].set_text('')
        state['source']=GLib.timeout_add_seconds(state['interval'],self.facility_auto_tick,kind);self.facility_auto_tick(kind)

    def facility_auto_tick(self,kind):
        state=self.facility_auto[kind]
        if self.window_closed or not state['enabled'] or not self.local_mode or self.local is not state['session']:
            state['source']=0;return False
        if state['running']:return True
        if self.facility_asset_sync_running:
            if not state['retry']:
                def retry():
                    state['retry']=0
                    if state['enabled']:self.facility_auto_tick(kind)
                    return False
                state['retry']=GLib.timeout_add(500,retry)
            return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;state['running']=True;self.facility_asset_sync_running=True
        label='Anlagen' if kind=='assets' else 'Wartungsaufträge';self.state.set_text(label+' werden automatisch abgeglichen …')
        def work():
            try:
                fn=sync_facility_assets if kind=='assets' else sync_maintenance
                result=fn(session,self.local_lock,lambda:self.local is session and self.local_mode and state['enabled'] and state['session'] is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,MaintenanceAssetPendingError)
            def done():
                state['running']=False;self.facility_asset_sync_running=False
                if self.window_closed or not state['enabled'] or self.local is not session or state['session'] is not session:return False
                if error:
                    if pending:state['error']='Wartungsaufträge warten auf den Abgleich ihrer Anlagen.';self.state.set_text('Wartung wartet auf Anlagenabgleich')
                    elif retryable:state['error']='Automatischer Abgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text(label+' warten auf Server')
                    else:self.stop_facility_auto_sync(kind);state['error']='Automatischer Abgleich angehalten: '+error;self.state.set_text(label+' angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    state['error']='';self.state.set_text(label+' automatisch abgeglichen')
                    if self.tab==5 and state['notice']:state['notice'].set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def start_facility_asset_sync(self):
        if not self.local_mode:return
        if self.facility_asset_sync_running:self.message('Ein Anlagenabgleich läuft bereits.');return
        values=self.form('Anlagenabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Facility-Anlagen synchronisieren?')
        confirm.format_secondary_text('Facility-Anlagen und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden. Wartungsaufträge können anschließend separat abgeglichen werden. Bei Konflikten bleiben Änderungen gespeichert.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.facility_asset_sync_running=True;self.state.set_text('Facility-Anlagen werden abgeglichen …')
        def work():
            try:result=sync_facility_assets(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.facility_asset_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Anlagenabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.facility_auto['assets']['error']='';self.state.set_text('Anlagenabgleich abgeschlossen')
                if self.tab==5 and self.facility_auto['assets']['notice']:self.facility_auto['assets']['notice'].set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Anlagenabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Facility-Anlagen übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_facility_asset_sync_conflict(self):
        if not self.local_mode:return
        if self.facility_asset_sync_running:self.message('Ein Anlagenabgleich läuft bereits.');return
        values=self.form('Anlagenkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.facility_asset_sync_running=True;self.state.set_text('Anlagenkonflikt wird geprüft …')
        def work():
            try:result=inspect_facility_asset_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.facility_asset_sync_running=False;return False
                if error:self.facility_asset_sync_running=False;self.state.set_text('Anlagenkonflikt nicht geprüft');self.message(error);return False
                def describe(v):return 'Name: '+v['name']+'\nKategorie: '+v['category']+'\nStandort: '+v['location']+'\nStatus: '+v['status']+'\nAktiv: '+('ja' if v['active'] else 'nein')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Anlagenkonflikt entscheiden')
                dialog.format_secondary_text('Lokal:\n'+describe(result['preview']['local'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie bisherigen Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.facility_asset_sync_running=False;self.state.set_text('Anlagenkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                def apply():
                    try:resolve_facility_asset_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.facility_asset_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Anlagenkonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Anlagenkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serveranlage übernommen' if accept else 'Lokale Anlage vorgemerkt');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_maintenance_sync(self):
        if not self.local_mode:return
        if self.facility_asset_sync_running:self.message('Ein Anlagen- oder Wartungsabgleich läuft bereits.');return
        values=self.form('Wartungsabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Wartungsaufträge synchronisieren?')
        confirm.format_secondary_text('Wartungsaufträge und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden. Die zugehörigen Anlagen müssen zuvor mit demselben Serverkonto abgeglichen sein. Bei Konflikten bleiben Änderungen gespeichert.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.facility_asset_sync_running=True;self.state.set_text('Wartungsaufträge werden abgeglichen …')
        def work():
            try:result=sync_maintenance(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.facility_asset_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Wartungsabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.facility_auto['maintenance']['error']='';self.state.set_text('Wartungsabgleich abgeschlossen')
                if self.tab==5 and self.facility_auto['maintenance']['notice']:self.facility_auto['maintenance']['notice'].set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Wartungsabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Wartungsaufträge übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_maintenance_sync_conflict(self):
        if not self.local_mode:return
        if self.facility_asset_sync_running:self.message('Ein Anlagen- oder Wartungsabgleich läuft bereits.');return
        values=self.form('Wartungskonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.facility_asset_sync_running=True;self.state.set_text('Wartungskonflikt wird geprüft …')
        def work():
            try:result=inspect_maintenance_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.facility_asset_sync_running=False;return False
                if error:self.facility_asset_sync_running=False;self.state.set_text('Wartungskonflikt nicht geprüft');self.message(error);return False
                def describe(v):return 'Titel: '+v['title']+'\nAnlagenkennung: '+v['assetId']+'\nStatus: '+v['status']+'\nFälligkeit: '+str(v['dueAt'])+'\nIntervall (Tage): '+str(v['recurrenceDays'])+'\nAktiv: '+('ja' if v['active'] else 'nein')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Wartungskonflikt entscheiden')
                duplicate=result['possibleDuplicateSuccessor'];local_candidates=result['preview']['successorDetails'];server_candidates=result['serverSuccessors']
                detail='Lokal:\n'+describe(result['preview']['localWire'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie bisherigen Änderungen werden protokolliert.'
                if duplicate:
                    if len(local_candidates)==1 and len(server_candidates)==1:
                        detail+='\n\nMöglicher lokaler Folgeauftrag:\n'+describe(local_candidates[0]['localWire'])+'\n\nMöglicher Serverfolgeauftrag:\n'+describe(server_candidates[0])+'\n\nDie gewählte Seite gilt auch für den Folgeauftrag. Beide Aufträge werden nach Bestätigung zugeordnet.'
                    else:detail+='\n\nEs gibt '+str(len(local_candidates))+' lokale und '+str(len(server_candidates))+' mögliche Serverfolgeaufträge. Eine eindeutige Zuordnung ist nicht möglich.'
                dialog.format_secondary_text(detail)
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.facility_asset_sync_running=False;self.state.set_text('Wartungskonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if duplicate:
                    if len(local_candidates)!=1 or len(server_candidates)!=1:self.facility_asset_sync_running=False;self.state.set_text('Wartungskonflikt unverändert');self.message('Bitte zuerst eine eindeutige Zuordnung der Folgeaufträge herstellen.');return False
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Folgeaufträge zuordnen?')
                    confirm.format_secondary_text('Der geprüfte lokale und der geprüfte Serverfolgeauftrag werden als derselbe Auftrag behandelt. '+('Der Serverstand ersetzt dabei den lokalen Folgeauftrag.' if accept else 'Lokale Änderungen am Folgeauftrag werden für den nächsten Abgleich vorgemerkt.')+' Die bisherigen Vorgänge werden protokolliert.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Zuordnen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.facility_asset_sync_running=False;self.state.set_text('Wartungskonflikt unverändert');return False
                def apply():
                    try:resolve_maintenance_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept,duplicate);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.facility_asset_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Wartungskonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Wartungskonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverauftrag übernommen' if accept else 'Lokaler Auftrag vorgemerkt');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_ticket_sync(self):
        if not self.local_mode:return
        if self.ticket_sync_running:self.message('Ein Ticketabgleich läuft bereits.');return
        values=self.form('Ticketabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Lokale Tickets synchronisieren?')
        confirm.format_secondary_text('Tickets und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden. Zuständige Personen müssen über den Mitarbeiterabgleich einem Serverkonto zugeordnet sein. Ticketkommentare und Anhänge können separat abgeglichen werden. Bei Konflikten bleibt die Warteschlange erhalten.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.ticket_sync_running=True;self.state.set_text('Tickets werden abgeglichen …')
        def work():
            try:result=sync_tickets(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.ticket_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Ticketabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.ticket_auto_error='';self.state.set_text('Ticketabgleich abgeschlossen')
                if self.tab==5 and self.ticket_auto_notice:self.ticket_auto_notice.set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Ticketabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Tickets übernommen.');dialog.run();dialog.destroy()
                if self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_ticket_sync_conflict(self):
        if not self.local_mode:return
        if self.ticket_sync_running:self.message('Ein Ticketabgleich läuft bereits.');return
        values=self.form('Ticketkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.ticket_sync_running=True;self.state.set_text('Ticketkonflikt wird geprüft …')
        def work():
            try:result=inspect_ticket_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.ticket_sync_running=False;return False
                if error:self.ticket_sync_running=False;self.state.set_text('Ticketkonflikt nicht geprüft');self.message(error);return False
                def describe(v):return 'Titel: '+v['title']+'\nKategorie: '+v['category']+'\nPriorität: '+v['priority']+'\nStatus: '+v['status']+'\nServerkonto: '+str(v['assigneeId'])+'\nAktiv: '+('ja' if v['active'] else 'nein')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Ticketkonflikt entscheiden')
                dialog.format_secondary_text('Lokal:\n'+describe(result['preview']['localWire'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie bisherigen Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.ticket_sync_running=False;self.state.set_text('Ticketkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                def apply():
                    try:resolve_ticket_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.ticket_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Ticketkonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Ticketkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverticket übernommen' if accept else 'Lokales Ticket vorgemerkt');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def stop_attachment_auto_sync(self,kind):
        state=self.attachment_auto[kind];state['enabled']=False;state['session']=None
        for key in ('source','retry'):
            if state[key]:GLib.source_remove(state[key]);state[key]=0

    def toggle_attachment_auto_sync(self,widget,kind):
        if not widget.get_active():self.stop_attachment_auto_sync(kind);return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und diesen Anhangsabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=attachment_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode,kind)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte diesen Anhangsabgleich für das Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        state=self.attachment_auto[kind];state['enabled']=True;state['session']=self.local;state['error']=''
        if self.tab==5 and state['notice']:state['notice'].set_text('')
        state['source']=GLib.timeout_add_seconds(state['interval'],self.attachment_auto_tick,kind);self.attachment_auto_tick(kind)

    def attachment_auto_tick(self,kind):
        state=self.attachment_auto[kind]
        if self.window_closed or not state['enabled'] or not self.local_mode or self.local is not state['session']:
            state['source']=0;return False
        if state['running']:return True
        if (self.task_sync_running if kind=='task' else self.ticket_sync_running):
            if not state['retry']:
                def retry():
                    state['retry']=0
                    if state['enabled']:self.attachment_auto_tick(kind)
                    return False
                state['retry']=GLib.timeout_add(500,retry)
            return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;state['running']=True
        if kind=='task':self.task_sync_running=True
        else:self.ticket_sync_running=True
        label='Aufgabenanhänge' if kind=='task' else 'Ticketanhänge';self.state.set_text(label+' werden automatisch abgeglichen …')
        def work():
            try:
                result=sync_attachments(session,self.local_lock,lambda:self.local is session and self.local_mode and state['enabled'] and state['session'] is session,CLIENT,url,token,ca,kind);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,AttachmentParentPendingError)
            def done():
                state['running']=False
                if kind=='task':self.task_sync_running=False
                else:self.ticket_sync_running=False
                if self.window_closed or not state['enabled'] or self.local is not session or state['session'] is not session:return False
                if error:
                    if pending:state['error']='Anhänge warten auf den Abgleich des zugehörigen Vorgangs.';self.state.set_text(label+' warten auf Vorgänge')
                    elif retryable:state['error']='Automatischer Anhangsabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text(label+' warten auf Server')
                    else:self.stop_attachment_auto_sync(kind);state['error']='Automatischer Anhangsabgleich angehalten: '+error;self.state.set_text('Anhangsabgleich angehalten · bitte prüfen')
                    if self.tab==5:self.refresh()
                else:
                    state['error']='';self.state.set_text(label+' automatisch abgeglichen')
                    if self.tab==5 and state['notice']:state['notice'].set_text('')
                    if result['received'] and self.tab in (0,2):self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def inspect_attachment_sync_conflict(self,kind):
        if not self.local_mode:return
        if (self.task_sync_running if kind=='task' else self.ticket_sync_running):self.message('Ein Abgleich dieses Bereichs läuft bereits.');return
        label='Aufgabenanhang' if kind=='task' else 'Ticketanhang'
        values=self.form(label+'skonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local
        if kind=='task':self.task_sync_running=True
        else:self.ticket_sync_running=True
        self.state.set_text('Anhangskonflikt wird geprüft …')
        def release():
            if kind=='task':self.task_sync_running=False
            else:self.ticket_sync_running=False
        def work():
            try:result=inspect_attachment_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,kind);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:release();return False
                if error:release();self.state.set_text('Anhangskonflikt nicht geprüft');self.message(error);return False
                local=result['preview']['local'];remote=result['remote']
                def describe(v):return 'Datei: '+v['originalName']+'\nGröße: '+str(v['sizeBytes'])+' Byte\nSHA-256: '+v['sha256']+'\nAutor: '+v['uploadedBy']+'\nAktiv: '+('ja' if v['active'] else 'nein')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Anhangskonflikt entscheiden')
                dialog.format_secondary_text('Lokal:\n'+describe(local)+'\n\nServer:\n'+describe(remote)+'\n\nDie bisherigen Änderungen werden protokolliert. Ein lokaler aktiver Stand kann beim nächsten Abgleich zentral wiederhergestellt werden, wenn der Server dies unterstützt.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):release();self.state.set_text('Anhangskonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                def apply():
                    try:resolve_attachment_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        release()
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Anhangskonflikt unverändert');self.message(problem);return False
                        self.state.set_text('Anhangskonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serveranhang übernommen' if accept else 'Lokaler Anhang vorgemerkt');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_attachment_sync(self,kind):
        if not self.local_mode:return
        if (self.task_sync_running if kind=='task' else self.ticket_sync_running):self.message('Ein Abgleich dieses Bereichs läuft bereits.');return
        label='Aufgabenanhänge' if kind=='task' else 'Ticketanhänge'
        values=self.form(label+' abgleichen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text=label+' synchronisieren?')
        confirm.format_secondary_text('Der zugehörige Aufgaben- bzw. Ticketabgleich und der Mitarbeiterabgleich müssen bereits für dasselbe Serverkonto eingerichtet sein. Die Originaldateien werden geprüft und an den Server übertragen; die ursprünglichen Autoren bleiben erhalten. Bei Konflikten oder fehlenden Dateien bleiben Änderungen vorgemerkt.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local
        if kind=='task':self.task_sync_running=True
        else:self.ticket_sync_running=True
        self.state.set_text(label+' werden abgeglichen …')
        def work():
            try:result=sync_attachments(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,kind);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                if kind=='task':self.task_sync_running=False
                else:self.ticket_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Anhangsabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.attachment_auto[kind]['error']='';self.state.set_text(label+' abgeglichen')
                if self.tab==5 and self.attachment_auto[kind]['notice']:self.attachment_auto[kind]['notice'].set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text=label+' abgeglichen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Änderungen übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def start_ticket_comment_sync(self):
        if not self.local_mode:return
        if self.ticket_sync_running:self.message('Ein Ticketabgleich läuft bereits.');return
        values=self.form('Ticketkommentare abgleichen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Ticketkommentare synchronisieren?')
        confirm.format_secondary_text('Tickets und Mitarbeiter müssen bereits für dasselbe Serverkonto abgeglichen und den Serverkonten zugeordnet sein. Vorhandene und spätere lokale Ticketkommentare werden an diesen Server gebunden. Autoren bleiben erhalten; das importierende Serverkonto wird protokolliert. Aufgabenkommentare werden gesondert abgeglichen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.ticket_sync_running=True;self.state.set_text('Ticketkommentare werden abgeglichen …')
        def work():
            try:result=sync_ticket_comments(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.ticket_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Kommentarabgleich angehalten');self.message(error+'\nAusstehende Kommentare bleiben gespeichert.');return False
                self.ticket_comment_auto_error='';self.state.set_text('Ticketkommentare abgeglichen')
                if self.tab==5 and self.ticket_comment_auto_notice:self.ticket_comment_auto_notice.set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Ticketkommentare abgeglichen')
                dialog.format_secondary_text(str(result['sent'])+' Kommentare übertragen, '+str(result['received'])+' Kommentare übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def start_task_comment_sync(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Aufgabenkommentare abgleichen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Aufgabenkommentare synchronisieren?')
        confirm.format_secondary_text('Aufgaben und Mitarbeiter müssen bereits für dasselbe Serverkonto abgeglichen und den Serverkonten zugeordnet sein. Vorhandene und spätere lokale Aufgabenkommentare werden an diesen Server gebunden. Autoren bleiben erhalten; das importierende Serverkonto wird protokolliert. Ticketkommentare werden nicht abgeglichen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;self.task_sync_running=True;self.state.set_text('Aufgabenkommentare werden abgeglichen …')
        def work():
            try:result=sync_task_comments(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.task_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Kommentarabgleich angehalten');self.message(error+'\nAusstehende Kommentare bleiben gespeichert.');return False
                self.state.set_text('Aufgabenkommentare abgeglichen')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Aufgabenkommentare abgeglichen')
                dialog.format_secondary_text(str(result['sent'])+' Kommentare übertragen, '+str(result['received'])+' Kommentare übernommen.');dialog.run();dialog.destroy();return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def start_task_sync(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Aufgabenabgleich zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Lokale Aufgaben synchronisieren?')
        confirm.format_secondary_text('Aufgaben und spätere Änderungen werden an diesen Server und dieses Serverkonto gebunden; lokale Aufgaben werden unter diesem Konto als Ersteller veröffentlicht. Zuständige Personen müssen durch den Mitarbeiterabgleich eindeutig einem Serverkonto zugeordnet sein. Aufgaben, Unteraufgaben und Voraussetzungen werden übertragen; Projektzuordnungen können separat abgeglichen werden; Aufgabenkommentare können separat abgeglichen werden; Freigaben und Anhänge können separat abgeglichen werden. Bei Konflikten bleibt der Abgleich stehen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca
        session=self.local;self.task_sync_running=True;self.state.set_text('Aufgaben werden abgeglichen …')
        def work():
            try:result=sync_tasks(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.task_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Aufgabenabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.task_auto_error='';self.state.set_text('Aufgabenabgleich abgeschlossen')
                if self.tab==5 and self.task_auto_notice:self.task_auto_notice.set_text('')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Aufgabenabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Aufgaben, Unteraufgaben oder Voraussetzungen übernommen. Weitere Beziehungen wurden nicht übertragen.');dialog.run();dialog.destroy()
                if self.tab==2:self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def inspect_dependency_sync_conflict(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Voraussetzungskonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.task_sync_running=True;self.state.set_text('Voraussetzungskonflikt wird geprüft …')
        def work():
            try:result=inspect_dependency_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.task_sync_running=False;return False
                if error:self.task_sync_running=False;self.state.set_text('Voraussetzungskonflikt nicht geprüft');self.message(error);return False
                local=result['preview'];remote=result['remote']
                def state(present):return 'vorhanden' if present else 'nicht vorhanden'
                text='Lokal:\nAufgabe: '+local['title']+' ('+local['status']+')\nVoraussetzung: '+local['dependencyTitle']+' ('+local['dependencyStatus']+')\nVerbindung: '+state(local['localPresent'])
                text+='\n\nServer:\nAufgabe: '+remote['task']['title']+' ('+remote['task']['status']+')\nVoraussetzung: '+remote['dependency']['title']+' ('+remote['dependency']['status']+')\nVerbindung: '+state(remote['present'])
                if remote['wouldCycle'] and local['localPresent'] and not remote['present']:text+='\n\nDer lokale Stand würde auf dem Server einen Zyklus erzeugen und kann erst nach Klärung der anderen Verbindung übertragen werden.'
                text+='\n\nDie bisherigen '+str(len(local['pending']))+' Änderungen werden protokolliert. Andere Voraussetzungen bleiben unverändert.'
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Voraussetzungskonflikt entscheiden');dialog.format_secondary_text(text)
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.task_sync_running=False;self.state.set_text('Voraussetzungskonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if accept:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Servervoraussetzung lokal übernehmen?')
                    confirm.format_secondary_text('Die geprüfte Serververbindung ersetzt die lokale Verbindung. Ihre ausstehenden Änderungen werden aus der Warteschlange entfernt und protokolliert.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.task_sync_running=False;self.state.set_text('Voraussetzungskonflikt unverändert');return False
                def apply():
                    try:resolve_dependency_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.task_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Voraussetzungskonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Voraussetzungskonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Servervoraussetzung übernommen' if accept else 'Lokale Voraussetzung vorgemerkt')
                        info.format_secondary_text('Der geprüfte Serverstand wurde lokal übernommen.' if accept else 'Der Serverstand wird beim nächsten Abgleich erneut geprüft.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def inspect_subtask_sync_conflict(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Unteraufgabenkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.task_sync_running=True;self.state.set_text('Unteraufgabenkonflikt wird geprüft …')
        def work():
            try:result=inspect_subtask_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.task_sync_running=False;return False
                if error:self.task_sync_running=False;self.state.set_text('Unteraufgabenkonflikt nicht geprüft');self.message(error);return False
                def describe(item):
                    try:due=datetime.fromtimestamp(item['dueAt']).strftime('%d.%m.%Y %H:%M') if item['dueAt'] else 'keine'
                    except (ValueError,OSError,OverflowError):due=str(item['dueAt'])+' (Unix-Zeit)'
                    return ('Titel: '+item['title']+'\nStatus: '+item['status']+'\nServerkonto: '+str(item['assigneeId'])+'\nFällig: '+due+'\nAktiv: '+('ja' if item['active'] else 'nein'))
                local=result['preview']['localWire'];remote=result['remote']
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Unteraufgabenkonflikt entscheiden')
                dialog.format_secondary_text(('Unabhängig angelegte Unteraufgaben: Mit der Entscheidung ordnen Sie diese lokale Unteraufgabe ausdrücklich der angezeigten Serverunteraufgabe zu.\n\n' if result.get('identityConflict') else '')+'Lokal:\n'+describe(local)+'\n\nServer:\n'+describe(remote)+'\n\nDer lokale Stand kann für den nächsten Abgleich vorgemerkt werden. Alternativ kann der Serverstand lokal übernommen werden. Die bisherigen '+str(len(result['preview']['pending']))+' Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.task_sync_running=False;self.state.set_text('Unteraufgabenkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if accept:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serverunteraufgabe lokal übernehmen?')
                    confirm.format_secondary_text('Der geprüfte Serverstand ersetzt diese lokale Unteraufgabe. Ihre ausstehenden Änderungen werden aus der Warteschlange entfernt und protokolliert.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.task_sync_running=False;self.state.set_text('Unteraufgabenkonflikt unverändert');return False
                def apply():
                    try:resolve_subtask_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.task_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Unteraufgabenkonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Unteraufgabenkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverunteraufgabe übernommen' if accept else 'Lokale Unteraufgabe vorgemerkt')
                        info.format_secondary_text('Der geprüfte Serverstand wurde lokal übernommen.' if accept else 'Der Serverstand wird beim nächsten Abgleich erneut geprüft.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def inspect_task_sync_conflict(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Aufgabenkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.task_sync_running=True;self.state.set_text('Aufgabenkonflikt wird geprüft …')
        def work():
            try:result=inspect_task_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.task_sync_running=False;return False
                if error:self.task_sync_running=False;self.state.set_text('Aufgabenkonflikt nicht geprüft');self.message(error);return False
                def describe(item,account):
                    try:due=datetime.fromtimestamp(item['dueAt']).strftime('%d.%m.%Y %H:%M') if item['dueAt'] else 'keine'
                    except (ValueError,OSError,OverflowError):due=str(item['dueAt'])+' (Unix-Zeit)'
                    return ('Titel: '+item['title']+'\nBereich: '+item['department']+'\nPriorität: '+item['priority']+'\nStatus: '+item['status']+'\nServerkonto: '+str(account)+'\nFällig: '+due+'\nWiederholung: '+str(item['recurrenceDays'])+' Tage\nAktiv: '+('ja' if item['active'] else 'nein'))
                local=result['preview']['local'];remote=result['remote']
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Aufgabenkonflikt entscheiden')
                dialog.format_secondary_text(('Unabhängig angelegte Aufgaben: Mit der Entscheidung ordnen Sie diese lokale Aufgabe ausdrücklich der angezeigten Serveraufgabe zu.\n\n' if result.get('identityConflict') else '')+'Lokal:\n'+describe(local,result['preview']['localServerAccount'])+'\n\nServer:\n'+describe(remote,remote['assigneeId'])+'\n\nDer lokale Stand kann für den nächsten Abgleich vorgemerkt werden. Alternativ kann der Serverstand lokal übernommen werden. Die bisherigen '+str(len(result['preview']['pending']))+' Änderungen werden protokolliert. Beziehungen und Anhänge werden nicht abgeglichen.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.task_sync_running=False;self.state.set_text('Aufgabenkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if accept:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serveraufgabe lokal übernehmen?')
                    confirm.format_secondary_text('Der geprüfte Serverstand ersetzt diese lokale Aufgabe. Ausstehende Änderungen werden aus der Warteschlange entfernt und protokolliert. Lokale Beziehungen und Anhänge bleiben bestehen.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.task_sync_running=False;self.state.set_text('Aufgabenkonflikt unverändert');return False
                def apply():
                    try:resolve_task_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.task_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Aufgabenkonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Aufgabenkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serveraufgabe übernommen' if accept else 'Lokale Aufgabe vorgemerkt')
                        info.format_secondary_text('Der geprüfte Serverstand wurde lokal übernommen.' if accept else 'Der Serverstand wird beim nächsten Abgleich erneut geprüft.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def stop_leave_auto_sync(self):
        self.leave_auto_enabled=False;self.leave_auto_session=None
        if self.leave_auto_source:
            GLib.source_remove(self.leave_auto_source);self.leave_auto_source=0

    def toggle_leave_auto_sync(self,widget):
        if not widget.get_active():self.stop_leave_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst eine lokale Sitzung öffnen und den Urlaubsabgleich einmal manuell einrichten.');return
        try:
            session=self.local;config=leave_sync_config(session,self.local_lock,lambda:self.local is session and self.local_mode)
            if not config or config['serverUrl']!=self.url.strip().rstrip('/'):
                raise LocalStoreError('Bitte den Urlaubsabgleich für dieses Serverkonto zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.leave_auto_enabled=True;self.leave_auto_session=self.local;self.leave_auto_error=''
        if self.tab==3 and self.leave_auto_notice:self.leave_auto_notice.set_text('')
        self.leave_auto_source=GLib.timeout_add_seconds(self.leave_auto_interval,self.leave_auto_tick);self.leave_auto_tick()

    def leave_auto_tick(self):
        if self.window_closed or not self.leave_auto_enabled or not self.local_mode or self.local is not self.leave_auto_session:
            self.leave_auto_source=0;return False
        if self.leave_sync_running:return True
        session=self.local;url=self.url;token=self.token;ca=self.ca;self.leave_sync_running=True;self.state.set_text('Automatischer Urlaubsabgleich …')
        def work():
            try:
                result=sync_leave(session,self.local_lock,lambda:self.local is session and self.local_mode and self.leave_auto_enabled and self.leave_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,LeaveRequestPendingError)
            def done():
                self.leave_sync_running=False
                if self.window_closed or not self.leave_auto_enabled or self.local is not session or self.leave_auto_session is not session:return False
                if error:
                    if pending:self.leave_auto_error='Automatischer Urlaubsabgleich wartet, bis die antragstellende Person ihren Antrag synchronisiert.';self.state.set_text('Urlaubsabgleich wartet auf Antrag')
                    elif retryable:self.leave_auto_error='Automatischer Urlaubsabgleich: Server vorübergehend nicht erreichbar; erneuter Versuch in 60 Sekunden.';self.state.set_text('Urlaubsabgleich wartet auf Server')
                    else:self.stop_leave_auto_sync();self.leave_auto_error='Automatischer Urlaubsabgleich angehalten: '+error;self.state.set_text('Urlaubsabgleich angehalten · bitte prüfen')
                    if self.tab==3:self.refresh()
                else:
                    self.leave_auto_error=(str(result['unmapped'])+' Anträge wurden wegen fehlender Zuordnung nicht übernommen.' if result['unmapped'] else '');self.state.set_text('Urlaubsabgleich automatisch aktiv')
                    if self.tab==3 and self.leave_auto_notice:self.leave_auto_notice.set_text(self.leave_auto_error)
                    if result['received'] and self.tab==3:self.refresh()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def inspect_incoming_leave_sync_conflict(self):
        if not self.local_mode:return
        if self.leave_sync_running:self.message('Ein Urlaubsabgleich läuft bereits.');return
        session=self.local;generation=self.generation
        values=self.form('Lokale Urlaubszuordnung prüfen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        if not self.local_mode or self.local is not session or self.leave_sync_running:self.message('Die Sitzung oder der Abgleich hat sich geändert.');return
        url,token,ca=values;self.leave_sync_running=True;self.state.set_text('Urlaubszuordnung wird geprüft …')
        def work():
            try:review=inspect_incoming_leave_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:review=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.leave_sync_running=False;return False
                if error:self.leave_sync_running=False;self.message(error);return False
                p=review['preview'];local=p['local'];remote=p['remote']
                def describe(item,employee,decider):
                    def date(value):
                        try:return datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M')
                        except (ValueError,OSError,OverflowError):return str(value)
                    return 'Person: '+employee+' · Serverkonto '+str(item['employeeId'])+'\nZeitraum: '+date(item['startsAt'])+' – '+date(item['endsAt'])+'\nGrund: '+item['reason']+'\nStatus: '+item['status']+' · Entscheidung: '+decider
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Lokale Urlaubszuordnung ersetzen?')
                dialog.format_secondary_text('Lokaler Stand:\n'+describe(local,p['localEmployeeName'],p['localDeciderName'])+'\n\nServerstand:\n'+describe(remote,p['remoteEmployeeName'],p['remoteDeciderName'])+'\n\nDer geprüfte Serverstand ersetzt den lokalen Stand; beide Fassungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response!=Gtk.ResponseType.OK:self.leave_sync_running=False;return False
                def apply():
                    try:accept_incoming_leave_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,review);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def done():
                        self.leave_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.message(problem);return False
                        self.state.set_text('Lokale Urlaubszuordnung aktualisiert');self.message('Der geprüfte Serverstand wurde lokal übernommen und protokolliert.')
                        if self.generation==generation:self.refresh()
                        return False
                    GLib.idle_add(done)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def inspect_leave_sync_conflict(self):
        if not self.local_mode:return
        if self.leave_sync_running:self.message('Ein Urlaubsabgleich läuft bereits.');return
        values=self.form('Urlaubskonflikt prüfen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.leave_sync_running=True;self.state.set_text('Urlaubskonflikt wird geprüft …')
        def work():
            try:result=inspect_leave_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.leave_sync_running=False;return False
                if error:self.leave_sync_running=False;self.state.set_text('Urlaubskonflikt nicht geprüft');self.message(error);return False
                def describe(item):
                    def date(value):
                        try:return datetime.fromtimestamp(value).strftime('%d.%m.%Y %H:%M')
                        except (ValueError,OSError,OverflowError):return str(value)+' (Unix-Zeit)'
                    return ('Antragsteller-Konto: '+str(item['employeeId'])+'\nBeginn: '+date(item['startsAt'])+'\nEnde: '+date(item['endsAt'])+'\nGrund: '+item['reason']+'\nStatus: '+item['status']+'\nEntscheider-Konto: '+str(item['decidedBy']))
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Urlaubskonflikt entscheiden')
                dialog.format_secondary_text(('Unabhängig angelegte Anträge: Mit der Bestätigung ordnen Sie Ihren lokalen Antrag ausdrücklich dem angezeigten Serverantrag zu.\n\n' if result.get('identityConflict') else '')+'Lokal:\n'+describe(result['preview']['local'])+'\n\nServer:\n'+describe(result['remote'])+'\n\nDie eigene ausstehende Änderung wird beim Übernehmen des Serverstands protokolliert und aus der Warteschlange entfernt. Ein bereits entschiedener Serverantrag kann nicht überschrieben werden.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response!=Gtk.ResponseType.OK:self.leave_sync_running=False;self.state.set_text('Urlaubskonflikt unverändert');return False
                confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serverantrag lokal übernehmen?')
                confirm.format_secondary_text('Der geprüfte Serverstand ersetzt den lokalen Antrag. Die bisherigen eigenen Änderungen bleiben im lokalen Protokoll erhalten.')
                confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                if decision!=Gtk.ResponseType.OK:self.leave_sync_running=False;self.state.set_text('Urlaubskonflikt unverändert');return False
                def apply():
                    try:accept_server_leave(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.leave_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Urlaubskonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Serverantrag übernommen')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverantrag übernommen');info.run();info.destroy()
                        if self.tab==3:self.refresh()
                        return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_leave_sync(self):
        if not self.local_mode:return
        if self.leave_sync_running:self.message('Ein Urlaubsabgleich läuft bereits.');return
        values=self.form('Urlaubsabgleich zum Server',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und persönliches Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Urlaub synchronisieren?')
        confirm.format_secondary_text('Die lokal angemeldete Person muss durch den Mitarbeiterabgleich ihrem persönlichen Serverkonto zugeordnet sein. Eigene Anträge und eigene Entscheidungen werden mit diesem Konto übertragen. Die Warteschlange wird an diesen Server gebunden; Tokens werden nicht gespeichert. Noch nicht zugeordnete Personen werden übersprungen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca
        session=self.local;self.leave_sync_running=True;self.state.set_text('Urlaub wird abgeglichen …')
        def work():
            try:result=sync_leave(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def finished():
                self.leave_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Urlaubsabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.state.set_text('Urlaubsabgleich abgeschlossen')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Urlaubsabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Anträge übernommen. Nicht zugeordnete Anträge: '+str(result['unmapped'])+'.');dialog.run();dialog.destroy()
                if self.tab==3:self.refresh()
                return False
            GLib.idle_add(finished)
        threading.Thread(target=work,daemon=True).start()

    def start_employee_sync(self):
        if not self.local_mode:return
        if self.employee_sync_running:self.message('Ein Mitarbeiterabgleich läuft bereits.');return
        values=self.form('Mitarbeiterübertragung zum Server',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Lokale Mitarbeiter synchronisieren?')
        confirm.format_secondary_text('Mitarbeiterverzeichnis und künftige Änderungen werden dauerhaft an diesen Server und dieses Serverkonto gebunden. Lokale Konten und Passwörter werden nicht übertragen. Dieser Mitarbeiterabgleich überträgt keinen Urlaub oder andere Fachbereiche. Bei Konflikten bleibt der Abgleich stehen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übertragen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca
        session=self.local;server_url=self.url;server_token=self.token;server_ca=self.ca;self.employee_sync_running=True;self.state.set_text('Mitarbeiter werden abgeglichen …')
        def work():
            try: result=sync_employees(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,server_url,server_token,server_ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc: result=None;error=str(exc)
            def finished():
                self.employee_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Mitarbeiterabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.')
                else:
                    self.state.set_text('Mitarbeiterabgleich abgeschlossen')
                    d=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Mitarbeiterabgleich abgeschlossen')
                    d.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Mitarbeiter übernommen. Urlaub und weitere Daten wurden nicht synchronisiert.');d.run();d.destroy()
                    if self.tab==3:self.refresh()
                return False
            GLib.idle_add(finished)
        threading.Thread(target=work,daemon=True).start()

    def inspect_employee_sync_conflict(self):
        if not self.local_mode:return
        if self.employee_sync_running:self.message('Ein Mitarbeiterabgleich läuft bereits.');return
        values=self.form('Mitarbeiterkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.employee_sync_running=True;self.state.set_text('Mitarbeiterkonflikt wird geprüft …')
        def work():
            try:result=inspect_employee_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.employee_sync_running=False;return False
                if error:self.employee_sync_running=False;self.state.set_text('Mitarbeiterkonflikt nicht geprüft');self.message(error);return False
                def describe(value):
                    return ('Name: '+value['name']+'\nTätigkeit: '+value['role']+'\nBereich: '+value['department']+'\nPräsenz: '+value['presence']+'\nStatus: '+('aktiv' if value['active'] else 'archiviert'))
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Mitarbeiterkonflikt entscheiden')
                identity_note='Die Einträge haben unterschiedliche Kennungen. Gleiche Namen beweisen nicht, dass es dieselbe Person ist. Bitte die Identität und das Serverkonto unabhängig prüfen.\n\n' if result.get('identityConflict') else ''
                dialog.format_secondary_text(identity_note+'Lokal:\n'+describe(result['preview']['local'])+'\n\nServer:\n'+describe(result['remote'])+'\nServerkonto-ID: '+str(result['remote']['accountEmployeeId'])+'\n\nDer lokale Stand kann für den nächsten Abgleich vorgemerkt werden; alternativ kann der Serverstand lokal übernommen werden. Die bisherigen '+str(result['preview']['count'])+' Änderungen werden protokolliert.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.employee_sync_running=False;self.state.set_text('Mitarbeiterkonflikt unverändert');return False
                accept=response==Gtk.ResponseType.YES
                if result.get('identityConflict'):
                    identity_confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Mitarbeiteridentität bestätigen?')
                    identity_confirm.format_secondary_text('Haben Sie unabhängig geprüft, dass der lokale Eintrag und der Servereintrag dieselbe Person sind? Das lokale Konto bleibt erhalten und wird dem Serverkonto '+str(result['remote']['accountEmployeeId'])+' zugeordnet. Die ursprüngliche Anlage wird dauerhaft gesperrt.')
                    identity_confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Ja, dieselbe Person',Gtk.ResponseType.OK);decision=identity_confirm.run();identity_confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.employee_sync_running=False;self.state.set_text('Mitarbeiterkonflikt unverändert');return False
                if accept:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Servermitarbeiter lokal übernehmen?')
                    confirm.format_secondary_text('Der Serverstand ersetzt diesen lokalen Mitarbeiter. Ausstehende Änderungen dieser Person werden aus der Warteschlange entfernt und protokolliert; lokale Konten und Passwörter bleiben erhalten.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.employee_sync_running=False;self.state.set_text('Mitarbeiterkonflikt unverändert');return False
                def apply():
                    try:resolve_employee_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result,accept);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.employee_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Mitarbeiterkonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Mitarbeiterkonflikt entschieden')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Servermitarbeiter übernommen' if accept else 'Lokaler Mitarbeiter vorgemerkt')
                        info.format_secondary_text('Der geprüfte Serverstand wurde lokal übernommen.' if accept else 'Der Serverstand wird beim nächsten Abgleich erneut geprüft.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def inspect_room_sync_conflict(self):
        if not self.local_mode:return
        if self.room_sync_running:self.message('Ein Raumabgleich läuft bereits.');return
        values=self.form('Raumkonflikt prüfen',[('Server-URL',self.url,False),('Bearer-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;self.room_sync_running=True;self.state.set_text('Raumkonflikt wird geprüft …')
        def work():
            try:result=inspect_room_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.room_sync_running=False;return False
                if error:self.room_sync_running=False;self.state.set_text('Raumkonflikt nicht geprüft');self.message(error);return False
                local=result['preview']['local'];remote=result['remote']
                def describe(room):return ('Name: '+room['name']+'\nStandort: '+room['location']+'\nKapazität: '+str(room['capacity'])+'\nAusstattung: '+room['equipment']+'\nStatus: '+('aktiv' if room['active'] else 'archiviert'))
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Raumkonflikt entscheiden')
                identity_note='Diese Räume haben unterschiedliche Kennungen. Nur fortfahren, wenn beide denselben Raum darstellen. Die ursprüngliche Raumanlage wird auf dem Server gesperrt.\n\n' if result.get('identityConflict') else ''
                dialog.format_secondary_text(identity_note+'Lokal:\n'+describe(local)+'\n\nServer:\n'+describe(remote)+'\n\nDer lokale Stand kann für den nächsten Raumabgleich vorgemerkt werden. Alternativ können Sie den Serverstand lokal übernehmen. Die bisherigen '+str(result['preview']['count'])+' ausstehenden Änderungen bleiben im lokalen Protokoll nachvollziehbar.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.YES,'Lokalen Stand vormerken',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response not in (Gtk.ResponseType.OK,Gtk.ResponseType.YES):self.room_sync_running=False;self.state.set_text('Raumkonflikt unverändert');return False
                accept_server=response==Gtk.ResponseType.YES
                if accept_server:
                    confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serverstand lokal übernehmen?')
                    confirm.format_secondary_text('Der angezeigte Serverstand ersetzt den lokalen Raum. '+str(result['preview']['count'])+' ausstehende lokale Änderungen für diesen Raum werden aus der Warteschlange entfernt und im lokalen Protokoll gesichert.')
                    confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);decision=confirm.run();confirm.destroy()
                    if decision!=Gtk.ResponseType.OK:self.room_sync_running=False;self.state.set_text('Raumkonflikt unverändert');return False
                def apply():
                    try:
                        action=accept_server_room_conflict if accept_server else rebase_room_conflict
                        action(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.room_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Raumkonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.state.set_text('Serverstand übernommen' if accept_server else 'Lokaler Raumstand vorgemerkt')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverstand übernommen' if accept_server else 'Lokaler Raumstand vorgemerkt')
                        info.format_secondary_text('Der lokale Raum enthält nun den geprüften Serverstand.' if accept_server else 'Beim nächsten Raumabgleich prüft der Server den Ausgangsstand noch einmal.');info.run();info.destroy();return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def restore_sync_running(self):
        return (self.room_sync_running or self.employee_sync_running or self.leave_sync_running or
                self.task_sync_running or self.project_sync_running or self.ticket_sync_running or
                self.organization_sync_running or self.facility_asset_sync_running or
                self.personal_task_comment_sync_running or self.personal_attachment_sync_running or
                any(state['running'] for state in self.attachment_auto.values()))

    def restore_database_backup(self):
        if not self.local_mode:return
        if self.restore_sync_running():self.message('Bitte warten Sie, bis der laufende Abgleich beendet ist.');return
        dialog=Gtk.FileChooserDialog(title='Datenbank wiederherstellen',transient_for=self,action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Auswählen',Gtk.ResponseType.OK);response=dialog.run();path=dialog.get_filename();dialog.destroy()
        if response!=Gtk.ResponseType.OK or not path:return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Datenbank wirklich wiederherstellen?')
        confirm.format_secondary_text('Die aktuelle Datenbank wird ersetzt. Eine Sicherung des bisherigen Zustands wird daneben angelegt. Schließen Sie zuvor andere Programme mit Zugriff auf die Datenbank. Anhänge werden nicht wiederhergestellt. Danach ist eine neue Anmeldung erforderlich.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Wiederherstellen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        if self.restore_sync_running():self.message('Bitte warten Sie, bis der laufende Abgleich beendet ist.');return
        self.stop_room_auto_sync();self.stop_booking_auto_sync();self.stop_employee_auto_sync();self.stop_presence_auto_sync(clear=True);self.stop_leave_auto_sync();self.stop_approval_auto_sync();self.stop_task_auto_sync();self.stop_project_auto_sync();self.stop_assignment_auto_sync();self.stop_comment_auto_sync();self.stop_ticket_auto_sync();self.stop_ticket_comment_auto_sync();self.stop_attachment_auto_sync('task');self.stop_attachment_auto_sync('ticket');self.stop_organization_auto_sync();self.stop_facility_auto_sync('assets');self.stop_facility_auto_sync('maintenance')
        session=self.local;self.set_sensitive(False)
        def done(result,error):
            self.set_sensitive(True)
            if self.local is not session:return
            with self.local_lock:
                if self.local is session:self.local.close();self.local=None;self.local_mode=False
            self.select(0)
            if error:self.message(error+'\nBitte melden Sie sich erneut an.');return
            info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Datenbank wiederhergestellt')
            info.format_secondary_text('Vorherige Datenbank: '+result['previous']+'\nBitte melden Sie sich erneut an. Anhänge müssen separat wiederhergestellt werden.');info.run();info.destroy()
        self.run_client(['-action','restore','-path',path],done)

    def central_admin_get(self,path,callback):
        """Authenticated, TLS-verified read-only admin calls without token in argv."""
        if self.local_mode or not self.url or not self.token:
            callback(None,'Keine zentrale Sitzung aktiv.');return
        credentials=(self.url,self.token,self.ca)
        def work():
            try:
                context=ssl.create_default_context(cafile=self.ca if self.ca else None)
                request=urllib.request.Request(self.url.rstrip('/')+path,headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
                with urllib.request.urlopen(request,timeout=15,context=context) as response:
                    if response.status!=200:raise ValueError('Serverantwort '+str(response.status))
                    payload=response.read(2*1024*1024+1)
                    if len(payload)>2*1024*1024:raise ValueError('Serverantwort zu groß.')
                    data=json.loads(payload)
                error=None
            except (OSError,ValueError,urllib.error.HTTPError) as exc:data=None;error=str(exc)
            def deliver():
                if credentials==(self.url,self.token,self.ca) and not self.local_mode:callback(data,error)
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=work,daemon=True).start()

    def central_admin_change(self,path,method,payload,callback):
        if self.local_mode or not self.url or not self.token:
            callback(None,'Keine zentrale Sitzung aktiv.');return
        credentials=(self.url,self.token,self.ca)
        def work():
            try:
                context=ssl.create_default_context(cafile=self.ca if self.ca else None)
                req=urllib.request.Request(self.url.rstrip('/')+path,data=json.dumps(payload).encode('utf-8'),method=method,headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Accept':'application/json'})
                with urllib.request.urlopen(req,timeout=20,context=context) as response:
                    data=json.loads(response.read(65536));error=None
            except (OSError,ValueError,urllib.error.HTTPError) as exc:data=None;error=str(exc)
            def deliver():
                if credentials==(self.url,self.token,self.ca) and not self.local_mode:callback(data,error)
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=work,daemon=True).start()

    def admin_input_dialog(self,title,fields):
        dialog=Gtk.Dialog(title=title,transient_for=self,modal=True)
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL)
        dialog.add_button('Speichern',Gtk.ResponseType.OK)
        area=dialog.get_content_area();area.set_spacing(8)
        controls={}
        for name,default in fields:
            area.pack_start(label(name,'muted'),False,False,0)
            widget=Gtk.Entry();widget.set_text(str(default));area.pack_start(widget,False,False,0)
            controls[name]=widget
        dialog.show_all();accepted=dialog.run()==Gtk.ResponseType.OK
        values={key:entry.get_text().strip() for key,entry in controls.items()} if accepted else None
        dialog.destroy();return values

    def show_managed_roles(self):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Serverrollen werden geladen …','muted'),False,False,0);self.render()
        def loaded(roles,error):
            if generation!=self.generation or self.local_mode:return
            self.clear();self.content.pack_start(label('Zentrale Rollenverwaltung','section'),False,False,0)
            self.content.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0)
            self.content.pack_start(label('Die Rollen gelten für serververwaltete Konten. Bestehende Umgebungskonten bleiben unverändert.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            def editor(name='',permissions=''):
                values=self.admin_input_dialog('Serverrolle', [('Rollenname',name),('Berechtigungen (Komma-getrennt)',permissions)])
                if not values:return
                role=values['Rollenname'];perms=[x.strip() for x in values['Berechtigungen (Komma-getrennt)'].split(',') if x.strip()]
                if not role or not perms:self.message('Rollenname und mindestens eine Berechtigung sind erforderlich.');return
                self.central_admin_change('/api/v1/admin/roles','PUT',{'name':role,'permissions':perms},lambda result,err:self.message(err) if err else self.show_managed_roles())
            self.content.pack_start(button('+ Neue Rolle',lambda _:editor()),False,False,0)
            for name,perms in sorted((roles or {}).items()):
                row=box(True,8);row.pack_start(label(name+' · '+', '.join(perms),'muted'),True,True,0)
                row.pack_start(button('Bearbeiten',lambda _,n=name,p=perms:editor(n,', '.join(p))),False,False,0)
                row.pack_start(button('Löschen',lambda _,n=name:self.central_admin_change('/api/v1/admin/roles','DELETE',{'name':n},lambda result,err:self.message(err) if err else self.show_managed_roles())),False,False,0)
                self.content.pack_start(row,False,False,0)
            self.render()
        self.central_admin_get('/api/v1/admin/roles',loaded)

    def show_managed_accounts(self):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Serverkonten werden geladen …','muted'),False,False,0);self.render()
        def loaded(accounts,error):
            if generation!=self.generation or self.local_mode:return
            self.clear();self.content.pack_start(label('Zentrale Kontenverwaltung','section'),False,False,0)
            self.content.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0)
            self.content.pack_start(label('Neue Konten werden einer Serverrolle zugeordnet. Das beim Anlegen erzeugte Token wird ausschließlich einmal angezeigt – bitte sicher übergeben.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            def create(_=None):
                values=self.admin_input_dialog('Serverkonto anlegen',[('Mitarbeiter-ID',''),('Rollenname','')])
                if not values:return
                try:identifier=int(values['Mitarbeiter-ID'])
                except ValueError:self.message('Ungültige Mitarbeiter-ID');return
                self.central_admin_change('/api/v1/admin/managed-accounts','POST',{'employeeId':identifier,'role':values['Rollenname']},lambda result,err:self.message(err) if err else (self.message('Zugangstoken (nur jetzt sichtbar):\n'+str(result.get('tokenOnce',''))),self.show_managed_accounts()))
            self.content.pack_start(button('+ Neues Serverkonto',create),False,False,0)
            for a in accounts or []:
                identifier=a['employeeId'];enabled=a['enabled'];name=str(identifier)+' · '+str(a['role'])+(' · Aktiv' if enabled else ' · Deaktiviert')
                row=box(True,8);row.pack_start(label(name,'muted'),True,True,0)
                def edit(_,id=identifier,current=a):
                    v=self.admin_input_dialog('Serverkonto bearbeiten',[('Rollenname',current['role']),('Aktiv (ja/nein)','ja' if current['enabled'] else 'nein')])
                    if v is None:return
                    active=v['Aktiv (ja/nein)'].lower() in ('ja','true','1')
                    self.central_admin_change('/api/v1/admin/managed-accounts','PUT',{'employeeId':id,'role':v['Rollenname'],'enabled':active},lambda result,err:self.message(err) if err else self.show_managed_accounts())
                row.pack_start(button('Bearbeiten',edit),False,False,0)
                def rotate(_,id=identifier):
                    self.central_admin_change('/api/v1/admin/managed-accounts/rotate','POST',{'employeeId':id},lambda result,err:self.message(err) if err else self.message('Neues Zugangstoken (nur jetzt sichtbar):\n'+str(result.get('tokenOnce',''))))
                row.pack_start(button('Token erneuern',rotate),False,False,0)
                row.pack_start(button('Löschen',lambda _,id=identifier:self.central_admin_change('/api/v1/admin/managed-accounts','DELETE',{'employeeId':id},lambda result,err:self.message(err) if err else self.show_managed_accounts())),False,False,0)
                self.content.pack_start(row,False,False,0)
            self.render()
        self.central_admin_get('/api/v1/admin/managed-accounts',loaded)

    def show_central_accounts(self):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Serverkonten werden geladen …','muted'),False,False,0);self.render()
        def loaded(entries,error):
            if generation!=self.generation or self.local_mode:return
            self.clear();h=box(False,8);h.pack_start(label('Zentrale Konten · Konfiguration','section'),True,True,0)
            h.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0);self.content.pack_start(h,False,False,0)
            self.content.pack_start(label('Diese Konten sind serverseitige Token-Identitäten, keine lokalen Linux-Konten. Zugangsdaten werden niemals angezeigt. Anlage und Änderung erfolgen derzeit ausschließlich in der Serverkonfiguration.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for item in entries or []:self.row('Mitarbeiter-ID '+str(item['employeeId']),str(item['source'])+' · Berechtigungen: '+', '.join(item['permissions']))
            if entries==[]:self.content.pack_start(label('Keine konfigurierten Serverkonten.','muted'),False,False,0)
            self.render()
        self.central_admin_get('/api/v1/admin/accounts',loaded)

    def show_central_permissions(self):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Serverberechtigungen werden geladen …','muted'),False,False,0);self.render()
        def loaded(entries,error):
            if generation!=self.generation or self.local_mode:return
            self.clear();h=box(False,8);h.pack_start(label('Zentrale Berechtigungen','section'),True,True,0)
            h.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0);self.content.pack_start(h,False,False,0)
            self.content.pack_start(label('Effektive Berechtigungskennungen aus der Serverkonfiguration. Ein zentraler, bearbeitbarer Rollenkatalog ist noch nicht vorhanden.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for entry in entries or []:self.row(entry,'Serverberechtigung')
            if entries==[]:self.content.pack_start(label('Keine Berechtigungen vorhanden.','muted'),False,False,0)
            self.render()
        self.central_admin_get('/api/v1/admin/permissions',loaded)

    def show_central_backup(self):
        if self.local_mode:return
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.YES_NO,text='Zentrale SQLite-Sicherung erstellen?')
        dialog.format_secondary_text('Der Server erzeugt eine konsistente Sicherung in seinem konfigurierten Sicherungsverzeichnis. Der Pfad kann nicht vom Client verändert werden.')
        approved=dialog.run()==Gtk.ResponseType.YES;dialog.destroy()
        if not approved:return
        credentials=(self.url,self.token,self.ca)
        def work():
            try:
                context=ssl.create_default_context(cafile=self.ca if self.ca else None)
                req=urllib.request.Request(self.url.rstrip('/')+'/api/v1/admin/backup',data=b'',method='POST',headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
                with urllib.request.urlopen(req,timeout=30,context=context) as response:
                    result=json.loads(response.read(8192))
                message='Server-Sicherung erstellt: '+str(result.get('filename',''))
            except (OSError,ValueError,urllib.error.HTTPError) as exc:message='Zentrale Sicherung fehlgeschlagen: '+str(exc)
            def deliver():
                if credentials==(self.url,self.token,self.ca) and not self.local_mode:self.message(message)
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=work,daemon=True).start()

    def show_central_event_history(self):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;connection=(self.url,self.token,self.ca);self.clear()
        self.content.pack_start(label('Server-Ereignisse werden geladen …','muted'),False,False,0);self.render()
        def loaded(entries,error):
            if generation!=self.generation or self.local_mode or connection!=(self.url,self.token,self.ca):return
            self.clear();header=box(False,8);header.pack_start(label('Server-Ereignisverlauf','section'),True,True,0)
            header.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0);self.content.pack_start(header,False,False,0)
            self.content.pack_start(label('Letzte 200 Ereignisse. Der Verlauf enthält weder Ereignisinhalte noch eine vollständige Benutzer- oder Auditdokumentation.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for entry in entries or []:self.content.pack_start(label(str(entry['id'])+' · '+str(entry['at'])+' · '+str(entry['topic']),'muted'),False,False,0)
            if entries==[]:self.content.pack_start(label('Keine Server-Ereignisse vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','central-event-history'],loaded)

    def show_audit(self):
        if not self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Audit wird geladen …','muted'),False,False,0);self.render()
        def loaded(entries,error):
            if generation!=self.generation or not self.local_mode:return
            self.clear();header=box(False,8);header.pack_start(label('Auditprotokoll','section'),True,True,0)
            header.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0);self.content.pack_start(header,False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for entry in entries or []:self.content.pack_start(label(entry,'muted'),False,False,0)
            if entries==[]:self.content.pack_start(label('Keine Audit-Einträge vorhanden.','muted'),False,False,0)
            def history_loaded(history,history_error):
                if generation!=self.generation or not self.local_mode:return
                self.content.pack_start(label('Mitarbeiter-Synchronisationsentscheidungen','section'),False,False,0)
                if history_error:self.content.pack_start(label(history_error,'muted'),False,False,0)
                for item in history or []:
                    choice='Serverstand übernommen' if item['choice']=='server' else 'Lokaler Stand vorgemerkt'
                    expander=Gtk.Expander(label=str(item['created_at'])+' · Mitarbeiter '+str(item['employee_id'])+' · '+choice)
                    detail='Lokaler Stand:\n'+item['local']+'\n\nServerstand:\n'+item['remote']+'\n\nFrühere Operationen:\n'+item['operations']
                    expander.add(label(detail,'muted'));self.content.pack_start(expander,False,False,0)
                if history==[]:self.content.pack_start(label('Keine Mitarbeiterentscheidungen vorhanden.','muted'),False,False,0)
                self.render()
            self.run_client(['-action','employee-resolution-history'],history_loaded)
            def room_history_loaded(history,history_error):
                if generation!=self.generation or not self.local_mode:return
                self.content.pack_start(label('Raum-Synchronisationsentscheidungen','section'),False,False,0)
                self.content.pack_start(label('Bei älteren Einträgen ist die Entscheidungsart nicht gespeichert.','muted'),False,False,0)
                if history_error:self.content.pack_start(label(history_error,'muted'),False,False,0)
                for item in history or []:
                    choice={'server':'Serverstand übernommen','local':'Lokaler Stand vorgemerkt'}.get(item['choice'],'Entscheidung unbekannt')
                    actor=('Mitarbeiter '+str(item['actor_employee_id'])) if item['actor_employee_id'] else str(item['actor'])
                    expander=Gtk.Expander(label=str(item['created_at'])+' · Raum '+str(item['room_id'])+' · '+actor+' · '+choice+(' · Identität zugeordnet' if item['identity_mapping'] else ''))
                    detail='Lokaler Stand:\n'+item['local']+'\n\nServerstand:\n'+item['remote']+'\n\nFrühere Operationen:\n'+item['operations']
                    expander.add(label(detail,'muted'));self.content.pack_start(expander,False,False,0)
                if history==[]:self.content.pack_start(label('Keine Raumentscheidungen vorhanden.','muted'),False,False,0)
                self.render()
            self.run_client(['-action','room-resolution-history'],room_history_loaded)
            def other_history_loaded(history,history_error):
                if generation!=self.generation or not self.local_mode:return
                self.content.pack_start(label('Weitere Synchronisationsentscheidungen','section'),False,False,0)
                if history_error:self.content.pack_start(label(history_error,'muted'),False,False,0)
                for item in history or []:
                    choice={'server':'Serverstand übernommen','local':'Lokaler Stand vorgemerkt'}.get(item['choice'],'Entscheidung unbekannt')
                    expander=Gtk.Expander(label=str(item['created_at'])+' · '+item['kind']+' · '+item['subject']+' · '+choice)
                    detail='Lokaler Stand:\n'+str(item['local'])+'\n\nServerstand:\n'+str(item['remote'])+'\n\nFrühere Operationen:\n'+str(item['operations'])
                    expander.add(label(detail,'muted'));self.content.pack_start(expander,False,False,0)
                if history==[]:self.content.pack_start(label('Keine weiteren Entscheidungen vorhanden.','muted'),False,False,0)
                self.render()
            self.run_client(['-action','sync-resolution-history'],other_history_loaded)
            self.render()
        self.run_client(['-action','audit'],loaded)

    def show_accounts(self):
        if not self.local_mode:return
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Konten werden geladen …','muted'),False,False,0);self.render()
        def loaded(accounts,error):
            if generation!=self.generation or not self.local_mode:return
            self.clear();header=box(False,8);header.pack_start(label('Lokale Konten','section'),True,True,0)
            header.pack_start(button('← Administration',lambda _:self.select(5)),False,False,0)
            header.pack_start(button('+ Konto',lambda _:self.account_form(),'primary'),False,False,0);self.content.pack_start(header,False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for account in accounts or []:
                status='Aktiv' if account['enabled'] else 'Deaktiviert'
                if account['mustChangePassword']:status+=' · Passwortwechsel erforderlich'
                if account['lockedUntil']>int(__import__('time').time()):status+=' · Vorübergehend gesperrt'
                actions=[] if account['isSelf'] else [('Passwort zurücksetzen',lambda _b,v=account:self.account_form(v))]
                if not account['isSelf']:actions.append(('Deaktivieren' if account['enabled'] else 'Aktivieren',lambda _b,v=account:self.toggle_account(v)))
                self.row(account['username'],f"{account['name']} · Mitarbeiter-ID {account['employeeId']} · {status}",extra=actions)
            if accounts==[]:self.content.pack_start(label('Keine Konten vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','accounts'],loaded)

    def account_action(self,args):
        generation=self.generation
        def done(_value,error):
            if generation!=self.generation or not self.local_mode:return
            if error:self.message(error)
            else:self.show_accounts()
        self.run_client(args,done)

    def account_form(self,account=None):
        fields=[] if account else [('Mitarbeiter-ID','',False),('Benutzername','',False)]
        fields += [('Temporäres Passwort (mindestens 12 Zeichen)','',True),('Passwort wiederholen','',True)]
        values=self.form('Passwort zurücksetzen' if account else 'Konto anlegen',fields)
        if values is None:return
        if values[-2]!=values[-1] or len(values[-2])<12:self.message('Die Passwörter müssen übereinstimmen und mindestens 12 Zeichen enthalten.');return
        if account:args=['-action','account-reset','-id',str(account['employeeId']),'-password',values[-2]]
        else:
            try:
                employee=int(values[0])
                if employee<=0:raise ValueError()
            except ValueError:self.message('Bitte eine gültige Mitarbeiter-ID eingeben.');return
            args=['-action','account-create','-id',str(employee),'-username',values[1],'-password',values[-2]]
        self.account_action(args)

    def toggle_account(self,account):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,
                                 text='Konto deaktivieren?' if account['enabled'] else 'Konto aktivieren?')
        dialog.format_secondary_text(account['username']);dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Bestätigen',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:
            self.account_action(['-action','account-enable','-id',str(account['employeeId']),'-enabled','0' if account['enabled'] else '1'])

    def change_own_password(self):
        values=self.form('Eigenes Passwort ändern',[('Aktuelles Passwort','',True),('Neues Passwort','',True),('Passwort wiederholen','',True)])
        if values is None:return
        if values[1]!=values[2] or len(values[1])<12:self.message('Die Passwörter müssen übereinstimmen und mindestens 12 Zeichen enthalten.');return
        self.run_client(['-action','password-change','-old',values[0],'-new',values[1]],lambda _value,error:self.finished(error))

    def show_organizations(self):
        mode=self.local_mode
        self.generation+=1;generation=self.generation;self.clear();self.content.pack_start(label('Organisation wird geladen …','muted'),False,False,0);self.render()
        def done(units,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8);header.pack_start(label('Organisationseinheiten','section'),True,True,0)
            header.pack_start(button('← Team',lambda _:self.select(3)),False,False,0)
            if mode:header.pack_start(button('Abgleiche öffnen',lambda _:self.select(5)),False,False,0)
            header.pack_start(button('+ Einheit',lambda _:self.organization_form(),'primary'),False,False,0);self.content.pack_start(header,False,False,0)
            self.content.pack_start(label('Lokale Einheiten können über Administration → Organisationseinheiten synchronisieren abgeglichen werden.' if mode else 'Zentrale Organisationseinheiten · Leitungs-ID bezeichnet eine Serverkonto-ID.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for unit in units or []:
                self.row(unit['name'],f"{unit['code']} · Leitungs-ID {unit['managerId']}",extra=[('Bearbeiten',lambda _b,v=unit:self.organization_form(v)),('Archivieren',lambda _b,v=unit:self.archive_organization(v))])
            if units==[]:self.content.pack_start(label('Keine Organisationseinheiten vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','organizations' if mode else 'central-organizations'],done)

    def organization_action(self,args):
        generation=self.generation;mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error:self.message(error)
            else:self.show_organizations()
        self.run_client(args,done)

    def organization_form(self,unit=None):
        unit=unit or {};fields=[('Name',unit.get('name',''),False),('Code',unit.get('code',''),False),('Leitungs-ID (0 = keine)' if self.local_mode else 'Serverkonto-ID der Leitung (0 = keine)',unit.get('managerId',0),False)]
        values=self.form('Einheit bearbeiten' if unit else 'Einheit anlegen',fields)
        if values is None:return
        try:
            manager=int(values[2])
            if manager<0:raise ValueError()
        except ValueError:self.message('Bitte eine gültige Leitungs-ID eingeben.');return
        self.organization_action(['-action',('organization-save' if self.local_mode else ('central-organization-update' if unit else 'central-organization-create')),'-id',str(unit.get('id',0) if self.local_mode else unit.get('id','')),'-name',values[0],'-code',values[1],'-manager-id',str(manager)])

    def archive_organization(self,unit):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Organisationseinheit archivieren?')
        dialog.format_secondary_text(unit['name']);dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:self.organization_action(['-action','organization-archive' if self.local_mode else 'central-organization-archive','-id',str(unit['id'])])

    def request_approval(self,task):
        values=self.form('Freigabe anfordern',[('Mitarbeiter-ID der freigebenden Person' if self.local_mode else 'Serverkonto-ID der freigebenden Person','',False)])
        if values is None:return
        try:
            approver=int(values[0])
            if approver<=0:raise ValueError()
        except ValueError:self.message('Bitte eine gültige Mitarbeiter-ID eingeben.');return
        if self.local_mode:self.task_detail_action(task,['-action','approval-create','-task-id',str(task['id']),'-approver-id',str(approver)])
        else:self.run_client(['-action','central-approval-request','-subject-id',str(task['id']),'-approver-id',str(approver)],lambda _value,error:self.finished(error))

    def show_approvals(self):
        mode=self.local_mode
        self.generation+=1;generation=self.generation;self.approval_view_generation=generation;self.clear();self.content.pack_start(label('Freigaben werden geladen …','muted'),False,False,0);self.render()
        def done(approvals,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8);header.pack_start(label('Meine Freigaben','section'),True,True,0)
            header.pack_start(button('← Aufgaben',lambda _:self.select(2)),False,False,0);self.content.pack_start(header,False,False,0)
            if mode:self.content.pack_start(action_flow([button('Freigabekonflikt prüfen',lambda _:self.inspect_approval_sync_conflict()),button('Freigaben synchronisieren',lambda _:self.start_approval_sync())]),False,False,0)
            self.content.pack_start(label('Eigene Anfragen und Entscheidungen können mit dem persönlichen Serverkonto abgeglichen werden.' if mode else 'Zentrale Freigaben für das angemeldete Serverkonto.','muted'),False,False,0)
            if mode:
                auto=Gtk.CheckButton(label='Freigaben automatisch alle 60 Sekunden abgleichen');auto.set_active(self.approval_auto_enabled);auto.connect('toggled',self.toggle_approval_auto_sync);self.content.pack_start(auto,False,False,0)
                if self.approval_auto_error:self.content.pack_start(label(self.approval_auto_error,'muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            names={'pending':'Offen','approved':'Genehmigt','rejected':'Abgelehnt'}
            for approval in approvals or []:
                detail=f"{approval.get('requesterName') or ('Serverkonto '+approval.get('requesterId','?'))} · {names.get(approval['status'],approval['status'])}"
                actions=[]
                if approval['status']=='pending':actions=[('Genehmigen',lambda _b,v=approval:self.decide_approval(v,'approved')),('Ablehnen',lambda _b,v=approval:self.decide_approval(v,'rejected'))]
                self.row(approval['taskTitle'] or ('Aufgabe '+str(approval['taskId'])),detail,extra=actions)
            if approvals==[]:self.content.pack_start(label('Keine Freigaben vorhanden.','muted'),False,False,0)
            self.render()
        self.run_client(['-action','approvals' if mode else 'central-approvals'],done)

    def stop_approval_auto_sync(self):
        self.approval_auto_enabled=False;self.approval_auto_session=None;self.approval_auto_credentials=None
        if self.approval_auto_source:
            GLib.source_remove(self.approval_auto_source);self.approval_auto_source=0

    def toggle_approval_auto_sync(self,widget):
        if not widget.get_active():self.stop_approval_auto_sync();return
        if not self.local_mode or not self.local or not self.url or not self.token:
            widget.set_active(False);self.message('Bitte zuerst den Freigabeabgleich manuell einrichten.');return
        session=self.local
        try:
            from leave_sync import transaction
            with transaction(session,self.local_lock,lambda:self.local is session and self.local_mode) as (db,_actor):
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='linux_approval_sync_config'").fetchone() or db.execute('SELECT server_url FROM linux_approval_sync_config').fetchone()!=(self.url.strip().rstrip('/'),):
                    raise LocalStoreError('Bitte den Freigabeabgleich für diesen Server zuerst manuell einrichten.')
        except LocalStoreError as exc:
            widget.set_active(False);self.message(str(exc));return
        self.approval_auto_enabled=True;self.approval_auto_session=session;self.approval_auto_credentials=(self.url,self.token,self.ca);self.approval_auto_error=''
        self.approval_auto_source=GLib.timeout_add_seconds(self.approval_auto_interval,self.approval_auto_tick);self.approval_auto_tick()

    def approval_auto_tick(self):
        if self.window_closed or not self.approval_auto_enabled or not self.local_mode or self.local is not self.approval_auto_session:
            self.approval_auto_source=0;return False
        if self.task_sync_running:return True
        session=self.local;url,token,ca=self.approval_auto_credentials;generation=self.generation;self.task_sync_running=True;self.state.set_text('Automatischer Freigabeabgleich …')
        def work():
            try:
                result=sync_approvals(session,self.local_lock,lambda:self.local is session and self.local_mode and self.approval_auto_enabled and self.approval_auto_session is session,CLIENT,url,token,ca);error=None;retryable=False;pending=False
            except (LocalStoreError,OSError,ValueError) as exc:
                result=None;error=str(exc);retryable=isinstance(exc,RoomSyncError) and exc.retryable;pending=isinstance(exc,ApprovalPendingError)
                if pending:result={'received':exc.received}
            def done():
                self.task_sync_running=False
                if self.window_closed or not self.approval_auto_enabled or self.local is not session or self.approval_auto_session is not session:return False
                old_error=self.approval_auto_error
                if error:
                    if pending:self.approval_auto_error='Freigabeabgleich wartet auf die zugehörige Aufgabe oder die antragstellende Person.';self.state.set_text('Freigabeabgleich wartet')
                    elif retryable:self.approval_auto_error='Server vorübergehend nicht erreichbar; erneuter Versuch folgt.';self.state.set_text('Freigabeabgleich wartet auf Server')
                    else:self.stop_approval_auto_sync();self.approval_auto_error='Automatischer Freigabeabgleich angehalten: '+error;self.state.set_text('Freigabeabgleich angehalten · bitte prüfen')
                else:
                    self.approval_auto_error=(str(result['unmapped'])+' Freigaben wegen fehlender Zuordnung nicht übernommen.' if result['unmapped'] else '');self.state.set_text('Freigabeabgleich automatisch aktiv')
                if self.generation==generation and self.approval_view_generation==generation and (old_error!=self.approval_auto_error or (result and result['received'])):self.show_approvals()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start();return True

    def inspect_approval_sync_conflict(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Freigabekonflikt prüfen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Prüfen')
        if values is None:return
        url,token,ca=values;session=self.local;generation=self.generation;self.task_sync_running=True;self.state.set_text('Freigabekonflikt wird geprüft …')
        def work():
            try:result=inspect_approval_conflict(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def show():
                if self.local is not session or not self.local_mode:self.task_sync_running=False;return False
                if error:self.task_sync_running=False;self.state.set_text('Freigabekonflikt nicht geprüft');self.message(error);return False
                local=result['preview']['local'];remote=result['remote']
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Freigabekonflikt entscheiden')
                dialog.format_secondary_text('Aufgabe: '+local['taskId']+'\nAntragsteller-Konto: '+local['requestedBy']+'\nFreigabe durch Konto: '+local['approverId']+'\n\nLokale Entscheidung: '+local['status']+'\nServerentscheidung: '+remote['status']+'\n\nDie eigene ausstehende Entscheidung wird beim Übernehmen des Serverstands protokolliert und aus der Warteschlange entfernt.')
                dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Serverstand übernehmen',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
                if response!=Gtk.ResponseType.OK:self.task_sync_running=False;self.state.set_text('Freigabekonflikt unverändert');return False
                confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Serverfreigabe lokal übernehmen?')
                confirm.format_secondary_text('Die geprüfte Serverentscheidung ersetzt die lokale Entscheidung. Die bisherige Änderung bleibt im lokalen Protokoll erhalten.')
                confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Übernehmen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
                if response!=Gtk.ResponseType.OK:self.task_sync_running=False;self.state.set_text('Freigabekonflikt unverändert');return False
                def apply():
                    try:accept_server_approval(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,result);problem=None
                    except (LocalStoreError,OSError,ValueError) as exc:problem=str(exc)
                    def completed():
                        self.task_sync_running=False
                        if self.local is not session or not self.local_mode:return False
                        if problem:self.state.set_text('Freigabekonflikt unverändert');self.message(problem);return False
                        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;self.approval_auto_error='';self.state.set_text('Serverfreigabe übernommen')
                        info=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Serverfreigabe übernommen');info.run();info.destroy()
                        if self.generation==generation:self.show_approvals()
                        return False
                    GLib.idle_add(completed)
                threading.Thread(target=apply,daemon=True).start();return False
            GLib.idle_add(show)
        threading.Thread(target=work,daemon=True).start()

    def start_approval_sync(self):
        if not self.local_mode:return
        if self.task_sync_running:self.message('Ein Aufgabenabgleich läuft bereits.');return
        values=self.form('Freigabeabgleich zum Server',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Weiter')
        if values is None:return
        url,token,ca=values
        if not url or not token:self.message('Bitte Server-URL und persönliches Token eingeben.');return
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Freigaben synchronisieren?')
        confirm.format_secondary_text('Die lokal angemeldete Person muss ihrem persönlichen Serverkonto zugeordnet sein. Eigene Anforderungen und Entscheidungen werden mit diesem Konto übertragen. Aufgaben und Mitarbeiter müssen zuvor abgeglichen sein. Tokens werden nicht gespeichert; unbekannte Zuordnungen werden übersprungen.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Abgleichen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        self.url,self.token,self.ca=url.strip().rstrip('/'),token,ca;session=self.local;generation=self.generation;self.task_sync_running=True;self.state.set_text('Freigaben werden abgeglichen …')
        def work():
            try:result=sync_approvals(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca);error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.task_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Freigabeabgleich angehalten');self.message(error+'\nAusstehende Änderungen bleiben gespeichert.');return False
                self.state.set_text('Freigabeabgleich abgeschlossen')
                dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.INFO,buttons=Gtk.ButtonsType.CLOSE,text='Freigabeabgleich abgeschlossen')
                dialog.format_secondary_text(str(result['sent'])+' Änderungen übertragen, '+str(result['received'])+' Freigaben übernommen. Nicht zugeordnet: '+str(result['unmapped'])+'.');dialog.run();dialog.destroy()
                if self.generation==generation:self.show_approvals()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def decide_approval(self,approval,status):
        generation=self.generation;mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error:self.message(error)
            else:self.show_approvals()
        self.run_client(['-action','approval-decision' if mode else 'central-approval-decision','-id',str(approval['id']),'-status',status],done)

    def employee_form(self,item=None):
        item=item or {}
        fields=[('Name',item.get('name',''),False),('Stellenbezeichnung',item.get('role',''),False),
                ('Abteilung',item.get('department',''),False)]
        roles=None
        if self.local_mode:
            try:
                with self.local_lock:
                    if self.local.identity().get('canManageRoles'):roles=self.local.roles()
            except LocalStoreError as exc:self.message(str(exc));return
        values=self.form('Mitarbeiter bearbeiten' if item else 'Mitarbeiter anlegen',fields,role_options=roles,role_selected=item.get('systemRole','')) if roles is not None else self.form('Mitarbeiter bearbeiten' if item else 'Mitarbeiter anlegen',fields)
        if values is None:return
        if self.local_mode:
            args=['-action','update-employee' if item else 'create-employee','-id',str(item.get('id',0)), '-name',values[0],'-role',values[1],'-department',values[2]]
            if len(values)>3 and values[3] and values[3]!=item.get('systemRole',''):args+=['-system-role',values[3]]
            self.run_client(args,lambda _value,error:self.finished(error))
        else:
            self.central_employee_save({'id':item.get('id',''),'name':values[0],'role':values[1],'department':values[2],'presence':item.get('presence','office')})

    def link_employee_account(self,employee):
        if self.local_mode:return
        generation=self.generation
        def loaded(accounts,error):
            if generation!=self.generation or self.local_mode:return
            if error:self.message(error);return
            dialog=Gtk.Dialog(title='Serverkonto zuordnen',transient_for=self,modal=True)
            dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Zuordnen',Gtk.ResponseType.OK)
            content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
            content.pack_start(label('Serverkonto für '+employee['name']),False,False,0)
            combo=Gtk.ComboBoxText();combo.append('0','Keine Zuordnung');current=str(employee.get('accountEmployeeId') or 0);current_available=current=='0'
            for account in accounts or []:
                if account['linkedEmployeeId'] and account['linkedEmployeeId']!=employee['id']:continue
                account_id=str(account['employeeId']);combo.append(account_id,'Konto '+account_id)
                if account_id==current:current_available=True
            if not current_available:combo.append(current,'Konto '+current+' (nicht mehr verfügbar)')
            combo.set_active_id(current)
            content.pack_start(combo,False,False,0);dialog.show_all();response=dialog.run();selected=combo.get_active_id();dialog.destroy()
            if response!=Gtk.ResponseType.OK or selected is None:return
            if selected==current:return
            confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Serverkonto-Zuordnung ändern?')
            confirm.format_secondary_text(employee['name']+' → '+('keine Zuordnung' if selected=='0' else 'Konto '+selected)+'. Die Server-Anmeldedaten und Berechtigungen selbst werden nicht geändert.')
            confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Zuordnen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
            if response==Gtk.ResponseType.OK:self.run_client(['-action','employee-account-link','-id',employee['id'],'-account-id',selected],lambda _value,problem:self.finished(problem))
        self.run_client(['-action','employee-accounts'],loaded)

    def central_employee_save(self,employee,presence=None):
        args=['-action','central-employee-update' if employee.get('id') else 'central-employee-create','-id',str(employee.get('id','')),
              '-name',employee['name'],'-role',employee['role'],'-department',employee['department'],'-presence',presence or employee.get('presence','office')]
        self.run_client(args,lambda _value,error:self.finished(error))

    def central_work_form(self,kind,item,can_manage_projects=False,edges=None):
        if kind=='task' and can_manage_projects and edges is None:
            generation=self.generation;connection=(self.url,self.token,self.ca)
            def loaded(value,error):
                if self.local_mode or self.generation!=generation or (self.url,self.token,self.ca)!=connection:return
                if error:self.message('Projektzuordnungen konnten nicht geladen werden: '+str(error));return
                self.central_work_form(kind,item,can_manage_projects,value or [])
            def capabilities(value,error):
                if self.local_mode or self.generation!=generation or (self.url,self.token,self.ca)!=connection:return
                if error:
                    if '404 Not Found' not in str(error):self.message('Serverfunktionen konnten nicht geladen werden: '+str(error));return
                    value={}
                if not isinstance(value,dict) or value.get('taskProjectUpdate')!=1:
                    self.state.set_text('Projektzuordnung im Aufgabenformular mit diesem Server nicht verfügbar')
                    self.central_work_form(kind,item,False,[]);return
                self.run_client(['-action','sync-project-assignments'],loaded)
            self.run_client(['-action','capabilities'],capabilities);return
        project_ids=[edge['projectId'] for edge in (edges or []) if edge.get('taskId')==item['id'] and isinstance(edge.get('projectId'),str)]
        field='department' if kind=='task' else 'category'
        fields=[('Titel',item.get('title',''),False),('Abteilung' if kind=='task' else 'Kategorie',item.get(field,''),False),
                ('Priorität (Niedrig, Mittel, Hoch, Kritisch)',item.get('priority','Mittel'),False),('Mitarbeiter-ID',item.get('assigneeId',0),False)]
        if kind=='task':
            try:due=datetime.fromtimestamp(item['dueAt']).strftime('%Y-%m-%d %H:%M') if item.get('dueAt') else ''
            except (ValueError,OSError,OverflowError):self.message('Die Fälligkeit kann nicht dargestellt werden.');return
            fields += [('Fällig (JJJJ-MM-TT SS:MM, leer = keine)',due,False),('Wiederholung in Tagen (0 = keine)',item.get('recurrenceDays',0),False)]
            if can_manage_projects and len(project_ids)<=1:fields.append(('Projekt-ID (0 = keine Zuordnung)',project_ids[0] if project_ids else '0',False))
        values=self.form('Aufgabe bearbeiten' if kind=='task' else 'Ticket bearbeiten',fields)
        if values is None:return
        try:
            if int(values[3])<=0:raise ValueError()
            if kind=='task':
                task_due=self.parse_local_time(values[4]) if values[4] else 0; repeat=int(values[5])
                if task_due<0 or not 0<=repeat<=3660:raise ValueError()
        except (ValueError,OverflowError,OSError):self.message('Bitte eine gültige Mitarbeiter-ID und Aufgabenplanung eingeben.');return
        args=['-action','update-'+kind,'-id',str(item['id']),'-title',values[0],'-'+field,values[1],'-priority',values[2],'-assignee-id',values[3]]
        if kind=='task':args+=['-due',str(task_due),'-recurrence',str(repeat)]
        if kind=='task' and can_manage_projects and len(project_ids)<=1:
            old=project_ids[0] if project_ids else '';new='' if values[6] in ('','0') else values[6]
            if old!=new:args+=['-expected-project-id',old,'-project-id',new]
        def saved(_value,error):
            if error and '-expected-project-id' in args and '409' in str(error):
                self.message('Die Projektzuordnung hat sich geändert oder das gewählte Projekt ist nicht mehr verfügbar. Bitte die Aufgabe erneut öffnen und die Zuordnung prüfen.\n'+str(error));self.refresh();return
            self.finished(error)
        self.run_client(args,saved)

    def local_ticket_form(self,item=None,defaults=None):
        editing=bool(item)
        if not editing:item=defaults
        item=item or {}
        assignee_default=item.get('assigneeId','')
        if not editing and not assignee_default:
            try:
                with self.local_lock:assignee_default=self.local.session_employee()
            except LocalStoreError as exc:self.message(str(exc));return
        fields=[('Titel',item.get('title',''),False),('Kategorie',item.get('category','IT'),False),
                ('Priorität (Niedrig, Mittel, Hoch, Kritisch)',item.get('priority','Mittel'),False),
                ('Mitarbeiter-ID',assignee_default,False)]
        values=self.form('Ticket bearbeiten' if editing else 'Ticket anlegen',fields)
        if values is None:return
        try:
            assignee=int(values[3])
            if assignee<=0:raise ValueError()
        except ValueError:
            self.message('Bitte eine gültige Mitarbeiter-ID eingeben.');return
        self.run_client(['-action','update-ticket' if editing else 'create-ticket','-id',str(item.get('id',0)),
            '-title',values[0],'-category',values[1],'-priority',values[2],'-assignee-id',str(assignee)],
            lambda _value,error:self.finished(error))

    def show_attachments(self,kind,item,include_archived=False):
        mode=self.local_mode
        self.generation+=1;generation=self.generation;self.clear()
        self.content.pack_start(label('Anhänge werden geladen …','muted'),False,False,0);self.render()
        identity={}
        def done(attachments,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8);manage=bool(identity.get('canManageTasks' if kind=='task' else 'canManageTickets'));can_write=manage or (identity.get('canCollaborate') and (not mode or identity.get('canReadTasks' if kind=='task' else 'canReadTickets')))
            header.pack_start(label('Anhänge · '+item.get('title',''),'section'),True,True,0)
            back=(lambda _:self.show_task_details(item)) if kind=='task' and mode else (lambda _:self.select(2 if kind=='task' else 4))
            header.pack_start(button('← Zurück',back),False,False,0)
            if can_write:header.pack_start(button('+ Datei',lambda _:self.choose_attachment(kind,item),'primary'),False,False,0)
            self.content.pack_start(header,False,False,0)
            actions=[]
            if mode:actions.append(button('Abgleiche öffnen',lambda _:self.select(5)))
            elif manage:actions.append(button('Nur aktive Anhänge' if include_archived else 'Archivierte Anhänge anzeigen',lambda _:self.show_attachments(kind,item,not include_archived)))
            if mode and identity.get('canReadTasks' if kind=='task' else 'canReadTickets'):actions.append(button('Anhänge persönlich abgleichen',lambda _:self.start_personal_attachment_sync(kind,item)))
            if actions:self.content.pack_start(action_flow(actions),False,False,0)
            self.content.pack_start(label('Eigene neue Dateien können übertragen und aktive Serveranhänge für diesen Vorgang übernommen werden. Die Administration richtet den Abgleich ein. Maximale Dateigröße: 100 MB.' if mode else 'Zentrale Anhänge · Maximale Dateigröße: 100 MB.','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for attachment in attachments or []:
                detail=f"{attachment['sizeBytes']} Bytes · SHA-256 {attachment['sha256']}"
                name=attachment['name'] if mode else attachment['originalName']
                if not attachment.get('active',True):
                    self.row(name,detail+' · Archiviert',extra=[('Wiederherstellen',lambda _b,v=attachment:self.restore_attachment(kind,item,v))] if manage else [])
                else:
                    actions=[('Öffnen' if mode else 'Herunterladen',lambda _b,v=attachment:self.open_attachment(kind,item,v))]
                    if can_write:actions.append(('Archivieren',lambda _b,v=attachment:self.archive_attachment(kind,item,v)))
                    self.row(name,detail,extra=actions)
            if attachments==[]:self.content.pack_start(label('Keine Anhänge vorhanden.','muted'),False,False,0)
            self.render()
        def got_identity(value,_error):
            if generation!=self.generation or self.local_mode!=mode:return
            identity.update(value or {})
            self.run_client(['-action','attachments','-subject',kind,'-id',str(item['id']),'-include-archived','1' if identity.get('canManageTasks' if kind=='task' else 'canManageTickets') else '0'] if mode else ['-action','subject-attachments-all' if include_archived else 'subject-attachments','-subject-type',kind,'-subject-id',str(item['id'])],done)
        self.run_client(['-action','identity'],got_identity)

    def start_personal_attachment_sync(self,kind,item):
        if not self.local_mode:return
        if self.personal_attachment_sync_running:self.message('Ein persönlicher Anhangsabgleich läuft bereits.');return
        session=self.local
        values=self.form('Anhänge persönlich abgleichen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Abgleichen')
        if values is None:return
        if not self.local_mode or self.local is not session:return
        url,token,ca=values;self.personal_attachment_sync_running=True;self.state.set_text('Eigene Anhänge werden übertragen …')
        def work():
            try:result=sync_personal_attachments(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,kind,int(item['id']));error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.personal_attachment_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Anhangsabgleich angehalten');self.message(error);return False
                self.state.set_text('Anhänge persönlich abgeglichen');self.message(str(result['sent'])+' eigene Anhangsänderungen übertragen, '+str(result['received'])+' Anhänge übernommen.');self.show_attachments(kind,item);return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def start_personal_task_comment_sync(self,item,kind='task'):
        if not self.local_mode:return
        if self.personal_task_comment_sync_running:self.message('Ein persönlicher Kommentarabgleich läuft bereits.');return
        session=self.local
        label_kind='Aufgaben' if kind=='task' else 'Ticket'
        values=self.form(label_kind+'kommentare persönlich abgleichen',[('Server-URL',self.url,False),('Persönliches Server-Token',self.token,True),('CA-Zertifikat (optional)',self.ca,False)],'Abgleichen')
        if values is None:return
        if not self.local_mode or self.local is not session:return
        url,token,ca=values;self.personal_task_comment_sync_running=True;self.state.set_text('Eigene Kommentare werden übertragen …')
        def work():
            try:result=(sync_personal_task_comments if kind=='task' else sync_personal_ticket_comments)(session,self.local_lock,lambda:self.local is session and self.local_mode,CLIENT,url,token,ca,int(item['id']));error=None
            except (LocalStoreError,OSError,ValueError) as exc:result=None;error=str(exc)
            def done():
                self.personal_task_comment_sync_running=False
                if self.local is not session or not self.local_mode:return False
                if error:self.state.set_text('Kommentarabgleich angehalten');self.message(error);return False
                self.state.set_text('Eigene Kommentare übertragen');self.message(str(result['sent'])+' eigene '+label_kind+'kommentare übertragen, '+str(result['received'])+' Kommentare übernommen.');self.show_comments(kind,item);return False
            GLib.idle_add(done)
        threading.Thread(target=work,daemon=True).start()

    def attachment_action(self,kind,item,args):
        generation=self.generation;mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error:self.message(error)
            else:self.show_attachments(kind,item)
        self.run_client(args,done)

    def choose_attachment(self,kind,item):
        dialog=Gtk.FileChooserDialog(title='Anhang auswählen',transient_for=self,action=Gtk.FileChooserAction.OPEN)
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Hinzufügen',Gtk.ResponseType.OK)
        response=dialog.run();path=dialog.get_filename();dialog.destroy()
        if response==Gtk.ResponseType.OK and path:
            self.attachment_action(kind,item,['-action','attachment-import','-subject',kind,'-id',str(item['id']),'-path',path] if self.local_mode else ['-action','subject-attachment-upload','-subject-type',kind,'-subject-id',str(item['id']),'-file',path])

    def open_attachment(self,kind,item,attachment):
        if not self.local_mode:
            dialog=Gtk.FileChooserDialog(title='Anhang herunterladen',transient_for=self,action=Gtk.FileChooserAction.SAVE)
            dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Herunterladen',Gtk.ResponseType.OK);dialog.set_current_name(attachment['originalName'])
            response=dialog.run();path=dialog.get_filename();dialog.destroy()
            if response==Gtk.ResponseType.OK and path:
                self.attachment_action(kind,item,['-action','subject-attachment-download','-subject-type',kind,'-subject-id',str(item['id']),'-id',str(attachment['id']),'-sha256',attachment['sha256'],'-size',str(attachment['sizeBytes']),'-file',path])
            return
        generation=self.generation
        def verified(value,error):
            if generation!=self.generation or not self.local_mode:return
            if error:self.message(error);return
            try:Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(value['path'],None),None)
            except GLib.Error as exc:self.message('Datei konnte nicht geöffnet werden: '+str(exc))
        self.run_client(['-action','attachment-verify','-subject',kind,'-id',str(item['id']),'-attachment-id',str(attachment['id'])],verified)

    def archive_attachment(self,kind,item,attachment):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Anhang archivieren?')
        dialog.format_secondary_text(attachment.get('name',attachment.get('originalName','')));dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:
            self.attachment_action(kind,item,['-action','attachment-archive','-subject',kind,'-id',str(item['id']),'-attachment-id',str(attachment['id'])] if self.local_mode else ['-action','subject-attachment-archive','-subject-type',kind,'-subject-id',str(item['id']),'-id',str(attachment['id'])])

    def restore_attachment(self,kind,item,attachment):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,buttons=Gtk.ButtonsType.NONE,text='Anhang wiederherstellen?')
        dialog.format_secondary_text((attachment.get('name') or attachment.get('originalName',''))+(' wird nach einer Integritätsprüfung lokal wieder aktiviert. Ein eingerichteter Abgleich überträgt die Änderung anschließend.' if self.local_mode else ' wird nach Prüfung des Serverstands und der Datei zentral wieder aktiviert.'))
        dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Wiederherstellen',Gtk.ResponseType.OK);response=dialog.run();dialog.destroy()
        if response==Gtk.ResponseType.OK:self.attachment_action(kind,item,['-action','attachment-restore','-subject',kind,'-id',str(item['id']),'-attachment-id',str(attachment['id'])] if self.local_mode else ['-action','subject-attachment-restore','-subject-type',kind,'-subject-id',str(item['id']),'-payload',json.dumps(attachment,ensure_ascii=False)])

    def show_comments(self,kind,item):
        mode=self.local_mode
        self.generation+=1; generation=self.generation; self.clear()
        self.content.pack_start(label('Verlauf wird geladen …','muted'),False,False,0);self.render()
        identity={}
        def done(comments,error):
            if generation!=self.generation or self.local_mode!=mode:return
            self.clear();header=box(False,8)
            header.pack_start(label('Verlauf · '+item.get('title',''),'section'),True,True,0)
            back=(lambda _:self.show_task_details(item)) if kind=='task' and mode else (lambda _:self.select(2 if kind=='task' else 4))
            header.pack_start(button('← Zurück',back),False,False,0)
            if identity.get('canManageTasks' if kind=='task' else 'canManageTickets') or (identity.get('canCollaborate') and (not mode or identity.get('canReadTasks' if kind=='task' else 'canReadTickets'))):header.pack_start(button('+ Kommentar',lambda _:self.comment_form(kind,item),'primary'),False,False,0)
            self.content.pack_start(header,False,False,0)
            actions=[]
            if mode:actions.append(button('Abgleiche öffnen',lambda _:self.select(5)))
            if mode and identity.get('canReadTasks' if kind=='task' else 'canReadTickets'):actions.append(button('Kommentare persönlich abgleichen',lambda _:self.start_personal_task_comment_sync(item,kind)))
            if actions:self.content.pack_start(action_flow(actions),False,False,0)
            self.content.pack_start(label(('Eigene Kommentare werden übertragen; Serverkommentare werden für diesen Vorgang übernommen. Die Administration richtet den Abgleich ein.') if mode else 'Zentraler Kommentarverlauf','muted'),False,False,0)
            if error:self.content.pack_start(label(error,'muted'),False,False,0)
            for comment in comments or []:
                author=comment.get('author') or ('Mitarbeiter #'+str(comment['authorId']))
                self.row(comment['body'],comment.get('createdAt','')+' · '+author)
            if comments==[]:self.content.pack_start(label('Noch keine Kommentare vorhanden.','muted'),False,False,0)
            self.render()
        def got_identity(value,_error):
            if generation!=self.generation or self.local_mode!=mode:return
            identity.update(value or {})
            self.run_client(['-action','comments','-subject' if mode else '-subject-type',kind,'-id' if mode else '-subject-id',str(item['id'])],done)
        self.run_client(['-action','identity'],got_identity)

    def comment_form(self,kind,item):
        dialog=Gtk.Dialog(title='Kommentar hinzufügen',transient_for=self,modal=True)
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL);dialog.add_button('Speichern',Gtk.ResponseType.OK)
        content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
        content.pack_start(label('Kommentar'),False,False,0)
        editor=Gtk.TextView();editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR);editor.set_size_request(430,120)
        content.pack_start(editor,True,True,0);dialog.set_default_size(480,230);dialog.show_all()
        response=dialog.run();buffer=editor.get_buffer();body=buffer.get_text(buffer.get_start_iter(),buffer.get_end_iter(),True);dialog.destroy()
        if response!=Gtk.ResponseType.OK:return
        if not body.strip():self.message('Bitte einen Kommentar eingeben.');return
        generation=self.generation;mode=self.local_mode
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=mode:return
            if error:self.message(error)
            else:self.show_comments(kind,item)
        args=['-action','comment-add','-subject',kind,'-id',str(item['id']),'-body',body] if mode else ['-action','add-comment','-subject-type',kind,'-subject-id',str(item['id']),'-body',body]
        self.run_client(args,done)

    def local_task_form(self,item=None):
        editing=bool(item);item=item or {}
        assignee=item.get('assigneeId',0)
        if not editing:
            try:
                with self.local_lock:assignee=self.local.session_employee()
            except LocalStoreError as exc:self.message(str(exc));return
        due=datetime.fromtimestamp(item['dueAt']).strftime('%Y-%m-%d %H:%M') if item.get('dueAt') else ''
        fields=[('Titel',item.get('title',''),False),('Abteilung',item.get('department','IT'),False),
                ('Priorität (Niedrig, Mittel, Hoch, Kritisch)',item.get('priority','Mittel'),False),
                ('Mitarbeiter-ID (0 = nicht zugewiesen)',assignee,False),
                ('Fällig (JJJJ-MM-TT SS:MM, leer = keine)',due,False),
                ('Wiederholung in Tagen (0 = keine)',item.get('recurrenceDays',0),False)]
        project_ids=item.get('projectIds',[]);single_project=not editing or len(project_ids)<=1
        if single_project:fields.append(('Projekt-ID (0 = keine Zuordnung)',project_ids[0] if project_ids else 0,False))
        values=self.form('Aufgabe bearbeiten' if item else 'Aufgabe anlegen',fields)
        if values is None:return
        try:
            assignee=int(values[3]); due_at=self.parse_local_time(values[4]) if values[4] else 0; recurrence=int(values[5])
            project=int(values[6]) if single_project else 0
            if assignee<0 or not 0<=recurrence<=3660 or project<0: raise ValueError()
        except (ValueError,OverflowError,OSError):
            self.message('Bitte gültige Mitarbeiter-ID, Fälligkeit und Wiederholung eingeben.');return
        args=['-action','update-task' if item else 'create-task','-id',str(item.get('id',0)),
              '-title',values[0],'-department',values[1],'-priority',values[2],'-assignee-id',str(assignee),
              '-due',str(due_at),'-recurrence',str(recurrence)]
        if single_project:
            args+=['-project-id',str(project)]
            if editing:args+=['-expected-project-id',str(project_ids[0] if project_ids else 0)]
        def saved(_value,error):
            if error and editing and 'Projektzuordnung prüfen und Ansicht aktualisieren' in str(error):
                self.message('Die Aufgabe konnte nicht gespeichert werden. Die Projektzuordnung wurde möglicherweise geändert oder das Projekt ist nicht mehr verfügbar. Bitte die Aufgabe erneut öffnen und prüfen.');self.refresh();return
            self.finished(error)
        self.run_client(args,saved)

    def assign_project(self,item):
        values=self.form('Projektzuordnung',[('Projekt-ID (0 = keine)',item.get('projectId',0),False)])
        if values is None:return
        try:
            project=int(values[0])
            if project<0:raise ValueError()
        except ValueError:
            self.message('Bitte eine gültige Projekt-ID eingeben.');return
        self.run_client(['-action','assign-project','-id',str(item['id']),'-project-id',str(project)],lambda _value,error:self.finished(error))

    def show_local_project_assignments(self,item):
        if not self.local_mode:return
        self.generation+=1;generation=self.generation;session=self.local;self.clear()
        self.content.pack_start(label('Projektzuordnungen werden geladen …','muted'),False,False,0);self.render()
        def current():return generation==self.generation and self.local_mode and self.local is session
        def tasks_loaded(tasks,error):
            if not current():return
            task=next((t for t in tasks or [] if t['id']==item['id']),None)
            if error or task is None:show([],None,error or 'Aufgabe nicht mehr vorhanden.');return
            def projects_loaded(projects,problem):
                if current():show(projects or [],task,problem)
            self.run_client(['-action','projects-for-assignment'],projects_loaded)
        def show(projects,task,error):
            if not current():return
            self.clear();header=box(False,8);header.pack_start(label('Projektzuordnungen · '+item.get('title',''),'section'),True,True,0)
            header.pack_start(button('← Aufgaben',lambda _:self.select(2)),False,False,0);self.content.pack_start(header,False,False,0)
            if error:self.content.pack_start(label(str(error),'muted'),False,False,0)
            else:
                assigned=set(task.get('projectIds',[]))
                shown=0
                for project in projects:
                    present=project['id'] in assigned
                    if not project.get('active',True) and not present:continue
                    shown+=1;self.row(project.get('name',''),project.get('department','')+(' · Archiviert' if not project.get('active',True) else '')+' · '+('Zugeordnet' if present else 'Nicht zugeordnet'),extra=[('Entfernen' if present else 'Zuordnen',lambda _b,p=project['id'],v=present:change(p,v))])
                if not shown:self.content.pack_start(label('Keine zuordenbaren Projekte vorhanden.','muted'),False,False,0)
            self.render()
        def change(project_id,present):
            def done(_value,error):
                if not current():return
                if error:self.message(str(error)+'\nDer aktuelle Stand wird erneut geladen.')
                self.show_local_project_assignments(item)
            self.run_client(['-action','project-edge','-id',str(item['id']),'-project-id',str(project_id),'-expected',str(int(present)),'-present',str(int(not present))],done)
        self.run_client(['-action','tasks'],tasks_loaded)

    def show_central_project_assignments(self,item):
        if self.local_mode:return
        self.generation+=1;generation=self.generation;connection=(self.url,self.token,self.ca);self.clear()
        self.content.pack_start(label('Projektzuordnungen werden geladen …','muted'),False,False,0);self.render()
        def current():return generation==self.generation and not self.local_mode and connection==(self.url,self.token,self.ca)
        def projects_loaded(projects,error):
            if not current():return
            if error:show([],[],error);return
            def assignments_loaded(edges,problem):
                if current():show(projects or [],edges or [],problem)
            self.run_client(['-action','sync-project-assignments'],assignments_loaded)
        def show(projects,edges,error):
            if not current():return
            self.clear();header=box(False,8);header.pack_start(label('Projektzuordnungen · '+item.get('title',''),'section'),True,True,0)
            header.pack_start(button('← Aufgaben',lambda _:self.select(2)),False,False,0);self.content.pack_start(header,False,False,0)
            if error:self.content.pack_start(label(str(error),'muted'),False,False,0)
            else:
                assigned={edge['projectId'] for edge in edges if edge.get('taskId')==item['id'] and isinstance(edge.get('projectId'),str)}
                shown=0
                for project in projects:
                    present=project['id'] in assigned
                    if not project.get('active',True) and not present:continue
                    shown+=1;self.row(project.get('name',''),project.get('department','')+(' · Archiviert' if not project.get('active',True) else '')+' · '+('Zugeordnet' if present else 'Nicht zugeordnet'),extra=[('Entfernen' if present else 'Zuordnen',lambda _b,p=project['id'],v=present:change(p,v))])
                if not shown:self.content.pack_start(label('Keine zuordenbaren Projekte vorhanden.','muted'),False,False,0)
            self.render()
        def change(project_id,present):
            payload={'clientId':'linux-ui-'+uuid.uuid4().hex,'operationId':uuid.uuid4().hex,'projectId':project_id,'taskId':item['id'],'present':not present,'expected':present}
            def done(_value,error):
                if not current():return
                if error:self.message(str(error)+'\nDer aktuelle Serverstand wird erneut geladen.')
                self.show_central_project_assignments(item)
            self.run_client(['-action','sync-project-assignment-event','-payload',json.dumps(payload,separators=(',',':'))],done)
        self.run_client(['-action','sync-projects'],projects_loaded)

    def reconcile_legacy_leave(self,item,employees):
        if self.local_mode or (item.get('identityVerified') is not False and item.get('identityOriginVerified') is not False):return
        mode=self.local_mode;connection=(self.url,self.token,self.ca);generation=self.generation
        people=[e for e in employees if isinstance(e.get('id'),str) and type(e.get('accountEmployeeId')) is int and e['accountEmployeeId']>0]
        if not people:self.message('Es sind keine Mitarbeiter mit zugeordnetem Serverkonto vorhanden.');return
        dialog=Gtk.Dialog(title='Urlaubszuordnung klären',transient_for=self,modal=True)
        dialog.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Weiter',Gtk.ResponseType.OK)
        content=dialog.get_content_area();content.set_border_width(20);content.set_spacing(8)
        identity_kind='Serverkonto-ID' if item.get('identityVerified') is True else 'bisherige lokale ID'
        content.pack_start(label('Historischer Antrag: '+item.get('reason','')+'\nAntragsteller ('+identity_kind+'): '+str(item.get('employeeId','?'))+'\nEntscheider ('+identity_kind+'): '+str(item.get('decidedBy',0))+'\nStatus: '+str(item.get('status','?'))),False,False,0)
        content.pack_start(label('Antragstellende Person auswählen:'),False,False,0)
        applicant=Gtk.ComboBoxText()
        for person in people:applicant.append(person['id'],person['name']+' · Konto '+str(person['accountEmployeeId']))
        content.pack_start(applicant,False,False,0);decider=None
        if item.get('status')!='pending':
            content.pack_start(label('Entscheidende Person auswählen:'),False,False,0);decider=Gtk.ComboBoxText()
            for person in people:decider.append(person['id'],person['name']+' · Konto '+str(person['accountEmployeeId']))
            content.pack_start(decider,False,False,0)
        dialog.show_all();response=dialog.run();employee_id=applicant.get_active_id();decider_id=decider.get_active_id() if decider else '';dialog.destroy()
        if response!=Gtk.ResponseType.OK:return
        if not employee_id or (decider is not None and (not decider_id or decider_id==employee_id)):
            self.message('Bitte die beteiligten unterschiedlichen Personen auswählen.');return
        if self.local_mode!=mode or (self.url,self.token,self.ca)!=connection or self.generation!=generation:self.message('Die Ansicht oder Verbindung wurde geändert. Bitte erneut prüfen.');return
        employee_name=next(e['name'] for e in people if e['id']==employee_id);decider_name=next((e['name'] for e in people if e['id']==decider_id),'Keine')
        confirm=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.WARNING,buttons=Gtk.ButtonsType.NONE,text='Urlaubszuordnung bestätigen?')
        confirm.format_secondary_text('Antrag: '+item.get('reason','')+'\nAntragstellende Person: '+employee_name+'\nEntscheidende Person: '+decider_name+'\n\nDer bisherige Datensatz und diese Zuordnung werden serverseitig protokolliert.')
        confirm.add_buttons('Abbrechen',Gtk.ResponseType.CANCEL,'Zuordnen',Gtk.ResponseType.OK);response=confirm.run();confirm.destroy()
        if response!=Gtk.ResponseType.OK:return
        if self.local_mode!=mode or (self.url,self.token,self.ca)!=connection or self.generation!=generation:self.message('Die Ansicht oder Verbindung wurde geändert. Bitte erneut prüfen.');return
        expected={k:v for k,v in item.items() if k not in ('identityVerified','identityOriginVerified','employeeExternalId','deciderExternalId')}
        payload={'expected':expected,'employeeExternalId':employee_id,'deciderExternalId':decider_id or ''}
        self.run_client(['-action','legacy-leave-reconcile','-payload',json.dumps(payload,ensure_ascii=False)],lambda _value,error:self.finished(error))

    def create_leave(self):
        day=datetime.now().date()+timedelta(days=1)
        values=self.form('Urlaubsantrag', [('Beginn (JJJJ-MM-TT SS:MM, Ortszeit)',day.strftime('%Y-%m-%d')+' 08:00',False),
            ('Ende (JJJJ-MM-TT SS:MM, Ortszeit)',day.strftime('%Y-%m-%d')+' 16:00',False),('Grund','Erholungsurlaub',False)])
        if values is None: return
        try:
            start=self.parse_local_time(values[0])
            end=self.parse_local_time(values[1])
            if start <= 0 or end <= start or not values[2]: raise ValueError()
        except (ValueError, OverflowError, OSError):
            self.message('Bitte gültige Daten, ein späteres Ende und einen Grund eingeben.'); return
        self.run_client(['-action','create-leave','-starts-at',str(int(start)),'-ends-at',str(int(end)),'-reason',values[2]],
                        lambda _value,error:self.finished(error))

    def decide_leave(self,item,decision,compare=None):
        if not self.local_mode and compare is None:
            generation=self.generation;connection=(self.url,self.token,self.ca)
            def capabilities(value,error):
                if self.local_mode or self.generation!=generation or (self.url,self.token,self.ca)!=connection:return
                if error and '404 Not Found' not in str(error):self.message('Serverfunktionen konnten nicht geladen werden: '+str(error));return
                supported=isinstance(value,dict) and value.get('leaveDecisionCompare')==1
                if not supported:self.state.set_text('Urlaubsvergleich mit diesem Server nicht verfügbar')
                self.decide_leave(item,decision,supported)
            self.run_client(['-action','capabilities'],capabilities);return
        generation=self.generation;local_mode=self.local_mode;session=self.local;connection=(self.url,self.token,self.ca)
        def done(_value,error):
            if generation!=self.generation or self.local_mode!=local_mode:return
            if local_mode and self.local is not session:return
            if not local_mode and connection!=(self.url,self.token,self.ca):return
            if error:self.message(str(error)+'\nDer aktuelle Stand wird erneut geladen.')
            self.refresh()
        args=['-action','leave-decision','-id',item['id'],'-status',decision]
        if local_mode or compare:args+=['-payload',json.dumps(item,ensure_ascii=False)]
        self.run_client(args,done)

    def advance_project_status(self,project):
        next_status={'planned':'active','active':'on_hold','on_hold':'completed','completed':'planned','cancelled':'planned'}.get(project.get('status'))
        if next_status is None:self.message('Der Projektstatus ist unbekannt.');return
        self.run_client(['-action','project-status','-id',str(project['id']),'-expected-status',project['status'],'-status',next_status],
                        lambda _value,error:self.finished(error))

    def edit(self, kind, item):
        if kind == 'room':
            fields = [('Name',item.get('name',''),False), ('Standort',item.get('location',''),False),
                      ('Kapazität',item.get('capacity',0),False), ('Ausstattung',item.get('equipment',''),False)]
            flags = ['-name','-location','-capacity','-equipment']
            title = 'Raum bearbeiten'
        else:
            fields = [('Name',item.get('name',''),False), ('Abteilung',item.get('department',''),False),
                      ('Verantwortliche Mitarbeiter-ID',item.get('ownerId',0),False),
                      ('Beschreibung',item.get('description',''),False),
                      ('Status (planned, active, on_hold, completed, cancelled)',item.get('status','planned'),False)]
            flags = ['-name','-department','-owner-id','-description','-status']
            title = 'Projekt bearbeiten'
        values = self.form(title, fields)
        if values is None: return
        args = ['-action','update-'+kind,'-id',item['id']]
        for flag,value in zip(flags,values): args.extend([flag,value])
        self.run_client(args,lambda _value,error:self.finished(error))

    def archive(self,kind,item):
        dialog=Gtk.MessageDialog(transient_for=self,modal=True,message_type=Gtk.MessageType.QUESTION,
                                 buttons=Gtk.ButtonsType.NONE,text='Eintrag archivieren?')
        dialog.format_secondary_text(item.get('name',item.get('title','')))
        dialog.add_button('Abbrechen',Gtk.ResponseType.CANCEL)
        dialog.add_button('Archivieren',Gtk.ResponseType.OK)
        response=dialog.run(); dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.run_client(['-action','central-employee-archive' if kind=='employee' and not self.local_mode else 'archive-'+kind,'-id',str(item['id'])],lambda _value,error:self.finished(error))

    def change_status(self,kind,id,status):
        self.run_client(['-action',kind+'-status','-id',id,'-status',status],lambda value,error:self.finished(error))
    def finished(self,error):
        if error: self.message(error)
        else: self.refresh()
    def create(self,kind,defaults=None):
        if self.local_mode and kind=='task': self.local_task_form(); return
        if self.local_mode and kind=='ticket': self.local_ticket_form(defaults=defaults); return
        defs={'task':('Aufgabe anlegen',[('Titel','',False),('Abteilung','IT',False),('Priorität','Mittel',False),('Mitarbeiter-ID','',False)],['-title','-department','-priority','-assignee-id']),
              'ticket':('Ticket anlegen',[('Titel','',False),('Kategorie','IT',False),('Priorität','Mittel',False),('Mitarbeiter-ID','',False)],['-title','-category','-priority','-assignee-id']),
              'room':('Raum anlegen',[('Name','',False),('Standort','',False),('Kapazität','',False),('Ausstattung','',False)],['-name','-location','-capacity','-equipment']),
              'project':('Projekt anlegen',[('Name','',False),('Abteilung','IT',False),('Verantwortliche Mitarbeiter-ID','',False),('Beschreibung','',False)],['-name','-department','-owner-id','-description'])}
        title,fields,flags=defs[kind]
        if kind=='ticket' and defaults:
            fields=[('Titel',defaults.get('title',''),False),('Kategorie',defaults.get('category','IT'),False),('Priorität','Mittel',False),('Mitarbeiter-ID',defaults.get('assigneeId',''),False)]
        if kind=='task' and defaults:fields[3]=('Mitarbeiter-ID',defaults.get('assigneeId',''),False)
        if kind=='project' and defaults:fields[2]=('Verantwortliche Mitarbeiter-ID',defaults.get('ownerId',''),False)
        if kind=='task':fields += [('Fällig (JJJJ-MM-TT SS:MM, leer = keine)','',False),('Wiederholung in Tagen (0 = keine)','0',False)]
        if kind=='task' and defaults and defaults.get('canManageProjects'):fields.append(('Projekt-ID (0 = keine Zuordnung)','0',False))
        values=self.form(title,fields)
        if values is None:return
        args=['-action','create-'+kind]
        for flag,value in zip(flags,values): args.extend([flag,value])
        if kind=='task':
            try:
                task_due=self.parse_local_time(values[4]) if values[4] else 0;repeat=int(values[5])
                if task_due<0 or not 0<=repeat<=3660:raise ValueError()
            except (ValueError,OverflowError,OSError):self.message('Bitte eine gültige Fälligkeit und Wiederholung eingeben.');return
            args+=['-due',str(task_due),'-recurrence',str(repeat)]
            if len(values)>6 and values[6] not in ('','0'):args+=['-project-id',values[6]]
        self.run_client(args,lambda value,error:self.finished(error))

if __name__=='__main__':
    win=AuraWindow(); win.show_all(); Gtk.main()
